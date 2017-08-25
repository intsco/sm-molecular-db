from app.log import get_logger
from app.model.molecule import Molecule
from app.config import ISOTOPE_CACHE_SIZE

import falcon
import numpy as np
import pyarrow.parquet
import pyarrow.ipc
import pandas as pd
import cpyMSpec as ms
import functools

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from itertools import product
from multiprocessing import cpu_count
import io
import pathlib
import subprocess

logger = get_logger()

ISOTOPIC_PEAK_N = 4
SIGMA_TO_FWHM = 2.3548200450309493  # 2 \sqrt{2 \log 2}

class InstrumentSettings(object):
    def __init__(self, pts_per_mz):
        self.pts_per_mz = pts_per_mz

    def __repr__(self):
        return "<Instrument(pts_per_mz={})>".format(self.pts_per_mz)

DECOY_ADDUCTS = ['+He', '+Li', '+Be', '+B', '+C', '+N', '+O', '+F', '+Ne', '+Mg', '+Al', '+Si', '+P', '+S', '+Cl', '+Ar', '+Ca', '+Sc', '+Ti', '+V', '+Cr', '+Mn', '+Fe', '+Co', '+Ni', '+Cu', '+Zn', '+Ga', '+Ge', '+As', '+Se', '+Br', '+Kr', '+Rb', '+Sr', '+Y', '+Zr', '+Nb', '+Mo', '+Ru', '+Rh', '+Pd', '+Ag', '+Cd', '+In', '+Sn', '+Sb', '+Te', '+I', '+Xe', '+Cs', '+Ba', '+La', '+Ce', '+Pr', '+Nd', '+Sm', '+Eu', '+Gd', '+Tb', '+Dy', '+Ho', '+Ir', '+Th', '+Pt', '+Os', '+Yb', '+Lu', '+Bi', '+Pb', '+Re', '+Tl', '+Tm', '+U', '+W', '+Au', '+Er', '+Hf', '+Hg', '+Ta']

def molecularFormulaSets(db_session):
    """
    returns dictionary {db_id => set of molecular formulas}
    """
    db_id_to_mfs = defaultdict(set)
    q = db_session.query(Molecule.sf, Molecule.db_id).distinct()
    for sf, db_id in q:
        db_id_to_mfs[int(db_id)].add(sf)
    return db_id_to_mfs

class IsotopePatternStorage(object):
    BUCKET = "sm-engine-isotope-patterns"
    BUCKET_PREFIX = "isotope_patterns"

    def __init__(self, mol_formulas, directory):
        self._dir = pathlib.Path(directory)
        self._dir.mkdir(exist_ok=True, parents=True)

        self._mf_cache = {}
        all_mfs = set()
        for db_id in mol_formulas:
            all_mfs |= mol_formulas[db_id]
            self._mf_cache[db_id] = pd.DataFrame({'mf': list(mol_formulas[db_id])})
        self._mf_cache[-1] = pd.DataFrame({'mf': list(all_mfs)})

    def _dir_path(self, instrument_settings, charge):
        return self._dir / "pts_{}".format(instrument_settings.pts_per_mz) / "charge_{}".format(charge)

    def _path(self, instrument_settings, adduct, charge):
        dir_path = self._dir_path(instrument_settings, charge)
        full_path = dir_path / "adduct_{}.parquet".format(adduct)
        return full_path

    def _load_single_file(self, filename):
        return pyarrow.parquet.read_table(str(filename)).to_pandas()

    # Caches Pandas dataframes for the last few instrument/polarity configurations.
    @functools.lru_cache(maxsize=ISOTOPE_CACHE_SIZE)
    def _load_dir(self, directory):
        # although pyarrow.parquet theoretically can read multiple files at once,
        # same Pandas indices seem to confuse it - some column values end up duplicated
        p = pathlib.Path(directory)
        dfs = [self._load_single_file(path) for path in p.iterdir()]
        return pd.concat(dfs)

    def _load(self, filename):
        p = pathlib.Path(filename)
        if p.is_dir():  # only one level is supported
            return self._load_dir(str(p))
        else:
            return self._load_single_file(filename)

    def _molecular_formulas(self, db_id=None):
        """
        db_id=None means all databases
        """
        if db_id is None:
            db_id = -1

        return self._mf_cache[int(db_id)]

    def _configurations(self):
        """
        Represents directory tree as a dataframe with pts_per_mz, charge, adduct columns
        """
        result = []
        for instr_dir in self._dir.iterdir():
            pts_per_mz = int(instr_dir.name.split('_')[1])
            for charge_dir in instr_dir.iterdir():
                charge = int(charge_dir.name.split('_')[1])
                for adduct_dir in charge_dir.iterdir():
                    adduct = adduct_dir.name.split('_')[1].split('.')[0]
                    result.append(dict(pts_per_mz=pts_per_mz, charge=charge, adduct=adduct))

        return pd.DataFrame.from_records(result)

    def adduct_charge_pairs(self):
        """
        Set of all (adduct, charge) combinations added so far
        """
        return {(x[1], x[2]) for x in self._configurations()[['adduct', 'charge']].itertuples()}

    def instrument_settings(self):
        """
        Set of all instrument settings added so far
        """
        return {InstrumentSettings(pts_per_mz=x) for x in self._configurations()['pts_per_mz'].unique()}

    def load_patterns(self, instrument_settings, charge, db_id=None, adducts=None):
        dir_path = self._dir_path(instrument_settings, charge)
        df = self._load(dir_path)
        if adducts:
            adducts_df = pd.DataFrame({'adduct': adducts})
            df = pd.merge(df, adducts_df)  # faster than .isin() method
        if not db_id:  # = all databases
            return df
        mol_formulas = self._molecular_formulas(db_id)
        return pd.merge(df, mol_formulas)

    def load_fdr_subsample(self, instrument_settings, charge, db_id,
                           target_adducts, decoy_adducts=DECOY_ADDUCTS, decoys_per_target=20,
                           random_seed=42):
        if random_seed:
            np.random.seed(random_seed)

        # pd.merge result is already grouped by the key, so sort is not needed
        df = self.load_patterns(instrument_settings, charge, db_id, target_adducts + decoy_adducts)

        df['is_target'] = df['adduct'].isin(target_adducts)
        decoy_df, target_df = [x[1] for x in df.groupby('is_target')]  # False < True
        n_decoy_adducts = len(decoy_df['adduct'].unique())
        n_mf = int(len(decoy_df) / n_decoy_adducts)
        assert decoys_per_target <= n_decoy_adducts

        # assert n_mf == len(decoy_df['mf'].unique())  # slows things down, left here for debugging

        # built-in np.random.choice is relatively slow for drawing samples without replacement,
        # generating a list of M random floats and taking .argsort()[:N] is much faster, as it turns out
        # http://numpy-discussion.10968.n7.nabble.com/Generating-random-samples-without-repeats-tp25666p25707.html
        def _select_decoys(adduct):
            selection = np.zeros_like(decoy_df['mf'], dtype=bool)
            for i in range(n_mf):
                sub_selection = np.random.rand(n_decoy_adducts).argsort()[:decoys_per_target]
                selection[i * n_decoy_adducts : (i+1) * n_decoy_adducts][sub_selection] = True
            return selection

        for adduct in target_adducts:
            target_df[adduct] = False
            decoy_df[adduct] = _select_decoys(adduct)

        # keep only decoy isotope patterns selected at least for one target adduct
        decoy_df = decoy_df[decoy_df[target_adducts].sum(axis=1) > 0]
        return pd.concat([target_df, decoy_df])

    def generate_patterns(self, instrument_settings, adduct, charge, db_id=None):
        """
        Fine-grained function for generating/updating a single file.
        db_id allows selecting a particular database, None value means all databases
        """
        dump_path = self._path(instrument_settings, adduct, charge)

        existing_df = None
        mf_finished = set()
        fn = str(dump_path)
        if dump_path.exists():
            existing_df = self._load(fn)
            mf_finished = set(existing_df['mf'])
            logger.info('read {} isotope patterns from {}'.format(len(mf_finished), fn))
        else:
            dump_path.parent.mkdir(exist_ok=True, parents=True)

        new_mol_formulas = list(self._molecular_formulas(db_id) - mf_finished)

        if not new_mol_formulas:
            logger.info('no new molecular formulas detected')
            return

        valid_mfs = []
        masses = []
        intensities = []
        for mf in new_mol_formulas:
            try:
                p = ms.isotopePattern(mf + adduct)
            except Exception as e:
                # logger.warning("skipped {}".format(mf) + ': ' + str(e))
                continue

            valid_mfs.append(mf)

            sigma = 5.0 / instrument_settings.pts_per_mz
            fwhm = sigma * SIGMA_TO_FWHM
            resolving_power = p.masses[0] / fwhm
            instrument_model = ms.InstrumentModel('tof', resolving_power)

            p.addCharge(charge)
            p = p.centroids(instrument_model).trimmed(ISOTOPIC_PEAK_N)
            p.sortByMass()
            masses.append(p.masses)
            intensities.append(p.intensities)

        df = pd.DataFrame({
            'mf': valid_mfs,
            'mzs': masses,
            'intensities': intensities
        })
        df['adduct'] = adduct
        if existing_df is not None and not existing_df.empty:
            df = pd.concat([existing_df, df])

        table = pyarrow.Table.from_pandas(df)
        pyarrow.parquet.write_table(table, fn)

        logger.info('wrote {} NEW isotope patterns to {}'.format(len(new_mol_formulas), fn))

    def batch_generate(self,
                       database_ids=None,
                       instrument_settings_list=None,
                       adduct_charge_pairs=None,
                       n_processes=None):
        """
        Generate isotope patterns for cartesian product of configurations
        Each parameter is either a list or None where the latter means taking all existing values.
        n_processes sets number of processes to use, None means use all cores.
        """
        param_tuples = product(database_ids or [None],
                               instrument_settings_list or self.instrument_settings(),
                               adduct_charge_pairs or self.adduct_charge_pairs())

        with ProcessPoolExecutor(max_workers=n_processes or cpu_count()) as executor:
            for params in param_tuples:
                adduct, charge = params[-1]
                db_id, instr = params[0:2]
                executor.submit(self.generate_patterns, instr, adduct, charge, db_id)

    def sync_from_s3(self, bucket=BUCKET, prefix=BUCKET_PREFIX):
        subprocess.check_output([
            "aws", "s3", "sync",
            "s3://{}/{}".format(bucket, prefix),
            self._dir])

    def sync_to_s3(self, bucket=BUCKET, prefix=BUCKET_PREFIX):
        subprocess.check_output([
            "aws", "s3", "sync",
            self._dir,
            "s3://{}/{}".format(bucket, prefix)])


def compute_all_patterns(pattern_storage):
    """
    Computes isotope patterns for all combinations of (molecular formula, adduct, charge, instrument settings)
    It's suggested to run this on a high-end machine (e.g. 64 threads) so that it finishes in less than 1 hour,
    and then call sync_to_s3 method to avoid repeating the calculations.
    """
    settings = [InstrumentSettings(pts_per_mz) for pts_per_mz in \
                [2019, 2885, 4039, 5770, 7212, 8078, 14425, 21637, 28850]]

    adduct_charge_pairs = []

    for adduct in ['+H', '+K', '+Na']:
        adduct_charge_pairs.append((adduct, 1))

    for adduct in ['-H', '+Cl']:
        adduct_charge_pairs.append((adduct, -1))

    for adduct in DECOY_ADDUCTS:
        adduct_charge_pairs.append((adduct, 1))
        adduct_charge_pairs.append((adduct, -1))

    pattern_storage.batch_generate(instrument_settings_list=settings,
                                   adduct_charge_pairs=adduct_charge_pairs)


class IsotopePatternCollection(object):
    def __init__(self, pattern_storage):
        self._storage = pattern_storage

    """
    /v1/isotope_patterns/{db_id}/{charge}/{pts_per_mz}

    NB: pyarrow is still somewhat flaky, always test if the client can read the data correctly;
    combination of Python 3.6.2 + pyarrow 0.6.0 should work, no guarantees about earlier versions.

    Example of usage on the client side:
    >>> import requests
    >>> import pyarrow.ipc
    >>> resp = requests.get('http://localhost:5001/v1/isotopic_patterns/3/-1/5770')
    >>> df = pyarrow.ipc.deserialize_pandas(resp.content)
    >>> resp.close()
    """
    def on_get(self, req, res, db_id, charge, pts_per_mz):
        db_session = req.context['session']
        instr = InstrumentSettings(int(pts_per_mz))
        patterns_df = self._storage.load_patterns(instr, int(charge), int(db_id))
        res.data = bytes(pyarrow.ipc.serialize_pandas(patterns_df))
        res.status = falcon.HTTP_200

class IsotopePatternFDRSubsample(object):
    def __init__(self, pattern_storage):
        self._storage = pattern_storage

    """
    'targets' parameter must contain comma-separated list of target adducts
    /v1/isotope_patterns_fdr/{db_id}/{charge}/{pts_per_mz}
    """
    def on_post(self, req, res, db_id, charge, pts_per_mz):
        # TODO improve error handling
        db_session = req.context['session']
        target_adducts = req.get_param('targets').split(',')
        decoy_adducts = req.get_param('decoys')
        if decoy_adducts:
            decoy_adducts = decoy_adducts.split(',')
        else:
            decoy_adducts = DECOY_ADDUCTS
        instr = InstrumentSettings(int(pts_per_mz))
        df = self._storage.load_fdr_subsample(instr, int(charge), int(db_id),
                                              target_adducts, decoy_adducts)
        res.data = bytes(pyarrow.ipc.serialize_pandas(df))
        res.status = falcon.HTTP_200