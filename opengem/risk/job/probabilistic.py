# pylint: disable-msg=W0232

""" Probabilistic Event Mixin: 

    Defines the behaviour of a Job. Calls the compute_risk task

"""

import math
import os

import numpy
from celery.exceptions import TimeoutError

from opengem import hazard
from opengem import job
from opengem import kvs
from opengem import logs
from opengem import producer
from opengem import risk
from opengem import settings
from opengem import shapes

from opengem.risk import probabilistic_event_based
from opengem.risk import job as risk_job
from opengem.output.risk import RiskXMLWriter
from opengem.parser import exposure
from opengem.parser import hazard as hazparser
from opengem.parser import vulnerability
from opengem.risk.job import output, RiskJobMixin


LOGGER = logs.LOG

DEFAULT_conditional_loss_poe = 0.01

def preload(fn):
    """ Preload decorator """
    def preloader(self, *args, **kwargs):
        """A decorator for preload steps that must run on the Jobber"""

        self.store_exposure_assets()
        self.store_vulnerability_model()

        return fn(self, *args, **kwargs)
    return preloader


class ProbabilisticEventMixin:
    """ Mixin for Probalistic Event Risk Job """

    @preload
    @output
    def execute(self):
        """ Execute a ProbabilisticLossRatio Job """

        results = []
        for block_id in self.blocks_keys:
            LOGGER.debug("starting task block, block_id = %s of %s" 
                        % (block_id, len(self.blocks_keys)))
            # pylint: disable-msg=E1101
            results.append(risk_job.compute_risk.delay(self.id, block_id))

        # task compute_risk has return value 'True' (writes its results to
        # memcache).
        for task in results:
            try:
                # TODO(chris): Figure out where to put that timeout.
                task.wait(timeout=None)
            except TimeoutError:
                # TODO(jmc): Cancel and respawn this task
                return None

    def slice_gmfs(self, block_id):
        """Load and collate GMF values for all sites in this block. """
        # TODO(JMC): Confirm this works regardless of the method of haz calc.
        histories = int(self['NUMBER_OF_SEISMICITY_HISTORIES'])
        realizations = int(self['NUMBER_OF_HAZARD_CURVE_CALCULATIONS'])
        num_ses = histories * realizations
        
        block = job.Block.from_kvs(block_id)
        sites_list = block.sites
        gmfs = {}
        for site in sites_list:
            risk_point = self.region.grid.point_at(site)
            key = "%s!%s" % (risk_point.row, risk_point.column)
            gmfs[key] = []
            
        for i in range(0, histories):
            for j in range(0, realizations):
                key = kvs.generate_product_key(
                        self.id, hazard.STOCHASTIC_SET_TOKEN, "%s!%s" % (i, j))
                fieldset = shapes.FieldSet.from_json(kvs.get(key), self.region.grid)
                for field in fieldset:
                    for key in gmfs.keys():
                        (row, col) = key.split("!")
                        gmfs[key].append(field.get(int(row), int(col)))
                                        
        for key, gmf_slice in gmfs.items():
            (row, col) = key.split("!")
            key_gmf = kvs.generate_product_key(self.id,
                risk.GMF_KEY_TOKEN, col, row)
            print "GMF_SLICE for %s X %s : \n\t%s" % (
                    col, row, gmf_slice )
            timespan = float(self['INVESTIGATION_TIME'])
            gmf = {"IMLs": gmf_slice, "TSES": num_ses * timespan, 
                    "TimeSpan": timespan}
            kvs.set_value_json_encoded(key_gmf, gmf)

    def store_exposure_assets(self):
        """ Load exposure assets and write to memcache """
        
        exposure_parser = exposure.ExposurePortfolioFile("%s/%s" % 
            (self.base_path, self.params[job.EXPOSURE]))

        for site, asset in exposure_parser.filter(self.region):
            gridpoint = self.region.grid.point_at(site)

            memcache_key_asset = kvs.generate_product_key(
                self.id, risk.EXPOSURE_KEY_TOKEN, 
                    gridpoint.column, gridpoint.row)

            LOGGER.debug("Loading asset %s at %s, %s" % (asset,
                site.longitude,  site.latitude))

            success = kvs.set_value_json_encoded(memcache_key_asset, asset)
            if not success:
                raise ValueError(
                    "jobber: cannot write asset to memcache")

    def store_vulnerability_model(self):
        """ load vulnerability and write to memcache """
        vulnerability.load_vulnerability_model(self.id,
            "%s/%s" % (self.base_path, self.params["VULNERABILITY"]))
    
    def compute_risk(self, block_id, **kwargs):
        """This task computes risk for a block of sites. It requires to have
        pre-initialized in memcache:
         1) list of sites
         2) gmfs
         3) exposure portfolio (=assets)
         4) vulnerability

        TODO(fab): make conditional_loss_poe (set of probabilities of exceedance
        for which the loss computation is done) a list of floats, and read it from
        the job configuration.
        """

        conditional_loss_poe = float(self.params.get(
                    'CONDITIONAL_LOSS_POE', 0.01))
        self.slice_gmfs(block_id)
        self.vuln_curves = \
                vulnerability.load_vulnerability_curves_from_kvs(self.job_id)

        # TODO(jmc): DONT assumes that hazard and risk grid are the same
        
        block = job.Block.from_kvs(block_id)
        sites_list = block.sites

        for site_idx, site in enumerate(sites_list):
            gridpoint = self.region.grid.point_at(site)

            LOGGER.debug("processing gridpoint %s, site %s" % (gridpoint, site_idx))
            loss_ratio_curve = self.compute_loss_ratio_curve(
                        gridpoint.column, gridpoint.row)
            print "Loss ratio curve for site %s is: \n\t %s" % (
                            site_idx, loss_ratio_curve)
            if loss_ratio_curve is not None:

                # write to memcache: loss_ratio
                key = kvs.generate_product_key(self.id,
                    risk.LOSS_RATIO_CURVE_KEY_TOKEN, gridpoint.column, gridpoint.row)

                LOGGER.debug("RESULT: loss ratio curve is %s, write to key %s" % (
                    loss_ratio_curve, key))
                kvs.set(key, loss_ratio_curve.to_json())
            
                # compute loss curve
                loss_curve = self.compute_loss_curve(gridpoint.column, gridpoint.row, 
                                                            loss_ratio_curve)
                key = kvs.generate_product_key(self.id, 
                    risk.LOSS_CURVE_KEY_TOKEN, gridpoint.column, gridpoint.row)

                print "RESULT: loss curve is %s, write to key %s" % (
                    loss_curve, key)
                kvs.set(key, loss_curve.to_json())
                loss_conditional = probabilistic_event_based. \
                            compute_conditional_loss(loss_curve, 
                                                        conditional_loss_poe)
                key = kvs.generate_product_key(self.id, 
                    risk.LOSS_TOKEN(conditional_loss_poe), gridpoint.column, gridpoint.row)

                print "RESULT: conditional loss is %s, write to key %s" % (
                    loss_conditional, key)
                kvs.set(key, loss_conditional)

        # assembling final product needs to be done by jobber, collecting the
        # results from all tasks
        return True

    def compute_loss_ratio_curve(self, column, row ): # site_id
        """Compute the loss ratio curve for a single site."""
        key_exposure = kvs.generate_product_key(self.job_id,
            risk.EXPOSURE_KEY_TOKEN, column, row)
        
        asset = kvs.get_value_json_decoded(key_exposure)

        vuln_function = self.vuln_curves[asset["VulnerabilityFunction"]]

        key = kvs.generate_product_key(self.job_id, 
                risk.GMF_KEY_TOKEN, column, row)
       
        gmf_slice = kvs.get_value_json_decoded(key)
        return probabilistic_event_based.compute_loss_ratio_curve(
                vuln_function, gmf_slice)

    def compute_loss_curve(self, column, row, loss_ratio_curve):
        """Compute the loss curve for a single site."""
        key_exposure = kvs.generate_product_key(self.job_id,
            risk.EXPOSURE_KEY_TOKEN, column, row)
        
        asset = kvs.get_value_json_decoded(key_exposure)
        
        if asset is None:
            return None
        
        return loss_ratio_curve.rescale_abscissae(asset["AssetValue"])


RiskJobMixin.register("Probabilistic Event", ProbabilisticEventMixin)