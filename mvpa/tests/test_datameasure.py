# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the PyMVPA package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Unit tests for PyMVPA SplittingSensitivityAnalyzer"""

import numpy as N

from mvpa.testing import *
from mvpa.testing.clfs import *
from mvpa.testing.datasets import *

from mvpa.base import externals, warning
from mvpa.featsel.base import FeatureSelectionPipeline, \
     SensitivityBasedFeatureSelection, CombinedFeatureSelection
from mvpa.featsel.helpers import FixedNElementTailSelector, \
                                 FractionTailSelector, RangeElementSelector

from mvpa.featsel.rfe import RFE

from mvpa.clfs.meta import SplitClassifier, MulticlassClassifier, \
     FeatureSelectionClassifier
from mvpa.clfs.smlr import SMLR, SMLRWeights
from mvpa.mappers.zscore import zscore
from mvpa.mappers.fx import sumofabs_sample, absolute_features, FxMapper, \
     maxofabs_sample
from mvpa.datasets.splitters import NFoldSplitter, NoneSplitter

from mvpa.misc.transformers import Absolute, \
     _second_axis_sum_of_abs, DistPValue

from mvpa.measures.base import SplitFeaturewiseDatasetMeasure
from mvpa.measures.anova import OneWayAnova, CompoundOneWayAnova
from mvpa.measures.irelief import IterativeRelief, IterativeReliefOnline, \
     IterativeRelief_Devel, IterativeReliefOnline_Devel


_MEASURES_2_SWEEP = [ OneWayAnova(),
                      CompoundOneWayAnova(postproc=sumofabs_sample()),
                      IterativeRelief(), IterativeReliefOnline(),
                      IterativeRelief_Devel(), IterativeReliefOnline_Devel()
                      ]
if externals.exists('scipy'):
    from mvpa.measures.corrcoef import CorrCoef
    _MEASURES_2_SWEEP += [ CorrCoef(),
                           # that one is good when small... handle later
                           #CorrCoef(pvalue=True)
                           ]

class SensitivityAnalysersTests(unittest.TestCase):

    def setUp(self):
        self.dataset = datasets['uni2large']


    @sweepargs(dsm=_MEASURES_2_SWEEP)
    def test_basic(self, dsm):
        data = datasets['dumbinv']
        datass = data.samples.copy()

        # compute scores
        f = dsm(data)
        # check if nothing evil is done to dataset
        self.failUnless(N.all(data.samples == datass))
        self.failUnless(f.shape == (1, data.nfeatures))
        self.failUnless(abs(f.samples[0, 1]) <= 1e-12, # some small value
            msg="Failed test with value %g instead of != 0.0" % f.samples[0, 1])
        self.failUnless(f[0] > 0.1)     # some reasonably large value

        # we should not have NaNs
        self.failUnless(not N.any(N.isnan(f)))



    # NOTE: lars with stepwise used to segfault if all ca are enabled
    @sweepargs(clfds=
               [(c, datasets['uni2large'])
                for c in clfswh['has_sensitivity', 'binary']] +
               [(c, datasets['uni4large'])
                for c in clfswh['has_sensitivity', 'multiclass']]
               )
    def test_analyzer_with_split_classifier(self, clfds):
        """Test analyzers in split classifier
        """
        clf, ds = clfds             # unroll the tuple
        # We need to skip some LARSes here
        _sclf = str(clf)
        if 'LARS(' in _sclf and "type='stepwise'" in _sclf:
            return

        # To don't waste too much time testing lets limit to 3 splits
        nsplits = 3
        splitter = NFoldSplitter(count=nsplits)
        mclf = SplitClassifier(clf=clf,
                               splitter=splitter,
                               enable_ca=['training_confusion',
                                              'confusion'])
        sana = mclf.get_sensitivity_analyzer(# postproc=absolute_features(),
                                           enable_ca=["sensitivities"])

        ulabels = ds.uniquetargets
        nlabels = len(ulabels)
        # Can't rely on splitcfg since count-limit is done in __call__
        assert(nsplits == len(list(splitter(ds))))
        sens = sana(ds)

        # It should return either ...
        #  nlabels * nsplits
        req_nsamples = [ nlabels * nsplits ]
        if nlabels == 2:
            # A single sensitivity in case of binary
            req_nsamples += [ nsplits ]
        else:
            # and for pairs in case of multiclass
            req_nsamples += [ (nlabels * (nlabels-1) / 2) * nsplits ]
            # Also for regression_based -- they can do multiclass
            # but only 1 sensitivity is provided
            if 'regression_based' in clf.__tags__:
                req_nsamples += [ nsplits ]

        # # of features should correspond
        self.failUnlessEqual(sens.shape[1], ds.nfeatures)
        # # of samples/sensitivities should also be reasonable
        self.failUnless(sens.shape[0] in req_nsamples)

        # Check if labels are present
        self.failUnless('splits' in sens.sa)
        self.failUnless('targets' in sens.sa)
        # should be 1D -- otherwise dtype object
        self.failUnless(sens.sa.targets.ndim == 1)

        sens_ulabels = sens.sa['targets'].unique
        # Some labels might be pairs(tuples) so ndarray would be of
        # dtype object and we would need to get them all
        if sens_ulabels.dtype is N.dtype('object'):
            sens_ulabels = N.unique(
                reduce(lambda x,y: x+y, [list(x) for x in sens_ulabels]))

        assert_array_equal(sens_ulabels, ds.sa['targets'].unique)

        errors = [x.percent_correct
                    for x in sana.clf.ca.confusion.matrices]

        # lets go through all sensitivities and see if we selected the right
        # features
        #if 'meta' in clf.__tags__ and len(sens.samples[0].nonzero()[0])<2:
        if '5%' in clf.descr \
               or (nlabels > 2 and 'regression_based' in clf.__tags__):
            # Some meta classifiers (5% of ANOVA) are too harsh ;-)
            # if we get less than 2 features with on-zero sensitivities we
            # cannot really test
            # Also -- regression based classifiers performance for multiclass
            # is expected to suck in general
            return

        if cfg.getboolean('tests', 'labile', default='yes'):
            for conf_matrix in [sana.clf.ca.training_confusion] \
                              + sana.clf.ca.confusion.matrices:
                self.failUnless(
                    conf_matrix.percent_correct>=70,
                    msg="We must have trained on each one more or " \
                    "less correctly. Got %f%% correct on %d labels" %
                    (conf_matrix.percent_correct,
                     nlabels))


        # Since  now we have per split and possibly per label -- lets just find
        # mean per each feature per label across splits
        sensm = FxMapper('samples', lambda x: N.sum(x),
                         uattrs=['targets'])(sens)
        sensgm = maxofabs_sample()(sensm)    # global max of abs of means

        assert_equal(sensgm.shape[0], 1)
        assert_equal(sensgm.shape[1], ds.nfeatures)

        selected = FixedNElementTailSelector(
            len(ds.a.bogus_features))(sensgm.samples[0])

        if cfg.getboolean('tests', 'labile', default='yes'):

            self.failUnlessEqual(
                set(selected), set(ds.a.nonbogus_features),
                msg="At the end we should have selected the right features. "
                "Chose %s whenever nonbogus are %s"
                % (selected, ds.a.nonbogus_features))

            # Now test each one per label
            # TODO: collect all failures and spit them out at once --
            #       that would make it easy to see if the sensitivity
            #       just has incorrect order of labels assigned
            for sens1 in sensm:
                labels1 = sens1.targets  # labels (1) for this sensitivity
                lndim = labels1.ndim
                label = labels1[0]      # current label

                # XXX whole lndim comparison should be gone after
                #     things get fixed and we arrive here with a tuple!
                if lndim == 1: # just a single label
                    self.failUnless(label in ulabels)

                    ilabel_all = N.where(ds.fa.targets == label)[0]
                    # should have just 1 feature for the label
                    self.failUnlessEqual(len(ilabel_all), 1)
                    ilabel = ilabel_all[0]

                    maxsensi = N.argmax(sens1) # index of max sensitivity
                    self.failUnlessEqual(maxsensi, ilabel,
                        "Maximal sensitivity for %s was found in %i whenever"
                        " original feature was %i for nonbogus features %s"
                        % (labels1, maxsensi, ilabel, ds.a.nonbogus_features))
                elif lndim == 2 and labels1.shape[1] == 2: # pair of labels
                    # we should have highest (in abs) coefficients in
                    # those two labels
                    maxsensi2 = N.argsort(N.abs(sens1))[0][-2:]
                    ilabel2 = [N.where(ds.fa.targets == l)[0][0]
                                    for l in label]
                    self.failUnlessEqual(
                        set(maxsensi2), set(ilabel2),
                        "Maximal sensitivity for %s was found in %s whenever"
                        " original features were %s for nonbogus features %s"
                        % (labels1, maxsensi2, ilabel2, ds.a.nonbogus_features))
                    """
                    # Now test for the sign of each one in pair ;) in
                    # all binary problems L1 (-1) -> L2(+1), then
                    # weights for L2 should be positive.  to test for
                    # L1 -- invert the sign
                    # We already know (if we haven't failed in previous test),
                    # that those 2 were the strongest -- so check only signs
                    """
                    self.failUnless(
                        sens1.samples[0, ilabel2[0]]<0,
                        "With %i classes in pair %s got feature %i for %r >= 0"
                        % (nlabels, label, ilabel2[0], label[0]))
                    self.failUnless(sens1.samples[0, ilabel2[1]]>0,
                        "With %i classes in pair %s got feature %i for %r <= 0"
                        % (nlabels, label, ilabel2[1], label[1]))
                else:
                    # yoh could be wrong at this assumption... time will show
                    self.fail("Got unknown number labels per sensitivity: %s."
                              " Should be either a single label or a pair"
                              % labels1)


    @sweepargs(clf=clfswh['has_sensitivity'])
    def test_mapped_classifier_sensitivity_analyzer(self, clf):
        """Test sensitivity of the mapped classifier
        """
        # Assuming many defaults it is as simple as
        mclf = FeatureSelectionClassifier(
            clf,
            SensitivityBasedFeatureSelection(
                OneWayAnova(),
                FractionTailSelector(0.5, mode='select', tail='upper')),
            enable_ca=['training_confusion'])

        sana = mclf.get_sensitivity_analyzer(postproc=sumofabs_sample(),
                                           enable_ca=["sensitivities"])
        # and lets look at all sensitivities

        dataset = datasets['uni2medium']
        # and we get sensitivity analyzer which works on splits
        sens = sana(dataset)
        self.failUnlessEqual(sens.shape, (1, dataset.nfeatures))



    @sweepargs(svm=clfswh['linear', 'svm'])
    def test_linear_svm_weights(self, svm):
        # assumming many defaults it is as simple as
        sana = svm.get_sensitivity_analyzer(enable_ca=["sensitivities"] )
        # and lets look at all sensitivities
        sens = sana(self.dataset)
        # for now we can do only linear SVM, so lets check if we raise
        # a concern
        svmnl = clfswh['non-linear', 'svm'][0]
        self.failUnlessRaises(NotImplementedError,
                              svmnl.get_sensitivity_analyzer)


    # XXX doesn't work easily with meta since it would need
    #     to be explicitely passed to the slave classifier's
    #     getSengetSensitivityAnalyzer
    # Note: only libsvm interface supports split_weights
    @sweepargs(svm=clfswh['linear', 'svm', 'libsvm', '!sg', '!meta'])
    def test_linear_svm_weights_per_class(self, svm):
        # assumming many defaults it is as simple as
        kwargs = dict(enable_ca=["sensitivities"])
        sana_split = svm.get_sensitivity_analyzer(
            split_weights=True, **kwargs)
        sana_full = svm.get_sensitivity_analyzer(
            force_training=False, **kwargs)

        # and lets look at all sensitivities
        ds2 = datasets['uni4large'].copy()
        zscore(ds2, param_est=('targets', ['L2', 'L3']))
        ds2 = ds2[N.logical_or(ds2.sa.targets == 'L0', ds2.sa.targets == 'L1')]

        senssplit = sana_split(ds2)
        sensfull = sana_full(ds2)

        self.failUnlessEqual(senssplit.shape, (2, ds2.nfeatures))
        self.failUnlessEqual(sensfull.shape,  (1, ds2.nfeatures))

        # just to verify that we split properly and if we reconstruct
        # manually we obtain the same
        dmap = (-1 * senssplit.samples[1]  + senssplit.samples[0]) \
               - sensfull.samples
        self.failUnless((N.abs(dmap) <= 1e-10).all())
        #print "____"
        #print senssplit
        #print SMLR().get_sensitivity_analyzer(combiner=None)(ds2)

        # for now we can do split weights for binary tasks only, so
        # lets check if we raise a concern
        # we temporarily shutdown warning, since it is going to complain
        # otherwise, but we do it on purpose here
        handlers = warning.handlers
        warning.handlers = []
        self.failUnlessRaises(NotImplementedError,
                              sana_split, datasets['uni3medium'])
        # reenable the warnings
        warning.handlers = handlers


    def test_split_featurewise_dataset_measure(self):
        ds = datasets['uni3small']
        sana = SplitFeaturewiseDatasetMeasure(
            analyzer=SMLR(
              fit_all_weights=True).get_sensitivity_analyzer(),
            splitter=NFoldSplitter(),
            )

        sens = sana(ds)
        # a sensitivity for each chunk and each label combination
        assert_equal(sens.shape,
                     (len(ds.sa['chunks'].unique) * len(ds.sa['targets'].unique),
                      ds.nfeatures))

        # Lets try more complex example with 'boosting'
        ds = datasets['uni3medium']
        ds.init_origids('samples')
        sana = SplitFeaturewiseDatasetMeasure(
            analyzer=SMLR(
              fit_all_weights=True).get_sensitivity_analyzer(),
            splitter=NoneSplitter(nperlabel=0.25, mode='first',
                                  nrunspersplit=2),
            enable_ca=['splits', 'sensitivities'])
        sens = sana(ds)

        assert_equal(sens.shape, (2 * len(ds.sa['targets'].unique),
                                  ds.nfeatures))
        splits = sana.ca.splits
        self.failUnlessEqual(len(splits), 2)
        self.failUnless(N.all([s[0].nsamples == ds.nsamples/4 for s in splits]))
        # should have used different samples
        self.failUnless(N.any([splits[0][0].sa.origids != splits[1][0].sa.origids]))
        # and should have got different sensitivities
        self.failUnless(N.any(sens[0] != sens[1]))


        #skip_if_no_external('scipy')
        # Let's disable this one for now until we are sure about the destiny of
        # DistPValue -- read the docstring of it!
        # Most evil example
        #ds = datasets['uni2medium']
        #plain_sana = SVM().get_sensitivity_analyzer(
        #       transformer=DistPValue())
        #boosted_sana = SplitFeaturewiseDatasetMeasure(
        #    analyzer=SVM().get_sensitivity_analyzer(
        #       transformer=DistPValue(fpp=0.05)),
        #    splitter=NoneSplitter(nperlabel=0.8, mode='first', nrunspersplit=2),
        #    enable_ca=['splits', 'sensitivities'])
        ## lets create feature selector
        #fsel = RangeElementSelector(upper=0.05, lower=0.95, inclusive=True)

        #sanas = dict(plain=plain_sana, boosted=boosted_sana)
        #for k,sana in sanas.iteritems():
        #    clf = FeatureSelectionClassifier(SVM(),
        #                SensitivityBasedFeatureSelection(sana, fsel),
        #                descr='SVM on p=0.01(both tails) using %s' % k)
        #    ce = CrossValidatedTransferError(TransferError(clf),
        #                                     NFoldSplitter())
        #    error = ce(ds)

        #sens = boosted_sana(ds)
        #sens_plain = plain_sana(ds)

        ## TODO: make a really unittest out of it -- not just runtime
        ##       bugs catcher

    # TODO -- unittests for sensitivity analyzers which use combiners
    # (linsvmweights for multi-class SVMs and smlrweights for SMLR)


    @sweepargs(basic_clf=clfswh['has_sensitivity'])
    ##REF: Name was automagically refactored
    def __test_fspipeline_with_split_classifier(self, basic_clf):
        #basic_clf = LinearNuSVMC()
        multi_clf = MulticlassClassifier(clf=basic_clf)
        #svm_weigths = LinearSVMWeights(svm)

        # Proper RFE: aggregate sensitivities across multiple splits,
        # but also due to multi class those need to be aggregated
        # somehow. Transfer error here should be 'leave-1-out' error
        # of split classifier itself
        sclf = SplitClassifier(clf=basic_clf)
        rfe = RFE(sensitivity_analyzer=
                    sclf.get_sensitivity_analyzer(
                        enable_ca=["sensitivities"]),
                  transfer_error=trans_error,
                  feature_selector=FeatureSelectionPipeline(
                      [FractionTailSelector(0.5),
                       FixedNElementTailSelector(1)]),
                  train_clf=True)

        # and we get sensitivity analyzer which works on splits and uses
        # sensitivity
        selected_features = rfe(self.dataset)

    def test_union_feature_selection(self):
        # two methods: 5% highes F-scores, non-zero SMLR weights
        fss = [SensitivityBasedFeatureSelection(
                    OneWayAnova(),
                    FractionTailSelector(0.05, mode='select', tail='upper')),
               SensitivityBasedFeatureSelection(
                    SMLRWeights(SMLR(lm=1, implementation="C"),
                                postproc=sumofabs_sample()),
                    RangeElementSelector(mode='select'))]

        fs = CombinedFeatureSelection(fss, combiner='union',
                                      enable_ca=['selected_ids',
                                                     'selections_ids'])

        od, otd = fs(self.dataset)

        self.failUnless(fs.combiner == 'union')
        self.failUnless(len(fs.ca.selections_ids))
        self.failUnless(len(fs.ca.selections_ids) <= self.dataset.nfeatures)
        # should store one set per methods
        self.failUnless(len(fs.ca.selections_ids) == len(fss))
        # no individual can be larger than union
        for s in fs.ca.selections_ids:
            self.failUnless(len(s) <= len(fs.ca.selected_ids))
        # check output dataset
        self.failUnless(od.nfeatures == len(fs.ca.selected_ids))
        for i, id in enumerate(fs.ca.selected_ids):
            self.failUnless((od.samples[:,i]
                             == self.dataset.samples[:,id]).all())

        # again for intersection
        fs = CombinedFeatureSelection(fss, combiner='intersection',
                                      enable_ca=['selected_ids',
                                                     'selections_ids'])
        # simply run it for now -- can't think of additional tests
        od, otd = fs(self.dataset)



def suite():
    return unittest.makeSuite(SensitivityAnalysersTests)


if __name__ == '__main__':
    import runner

