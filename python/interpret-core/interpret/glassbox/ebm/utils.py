# Copyright (c) 2019 Microsoft Corporation
# Distributed under the MIT software license

# TODO: Test EBMUtils

from math import ceil, isnan, isinf, exp, log
from .internal import Native, Booster, InteractionDetector

# from scipy.special import expit
from sklearn.utils.extmath import softmax
from sklearn.model_selection import train_test_split
from sklearn.base import is_classifier
import numbers
import numpy as np
import warnings
import copy
import operator
from itertools import islice

from scipy.stats import norm
from scipy.optimize import root_scalar, brentq

from .postprocessing import multiclass_postprocess2

from itertools import count, chain

import logging

_log = logging.getLogger(__name__)

def _zero_tensor(tensor, zero_low=None, zero_high=None):
    entire_tensor = [slice(None) for _ in range(tensor.ndim)]
    if zero_low is not None:
        for dimension_idx, is_zero in enumerate(zero_low):
            if is_zero:
                dim_slices = entire_tensor.copy()
                dim_slices[dimension_idx] = 0
                tensor[tuple(dim_slices)] = 0
    if zero_high is not None:
        for dimension_idx, is_zero in enumerate(zero_high):
            if is_zero:
                dim_slices = entire_tensor.copy()
                dim_slices[dimension_idx] = -1
                tensor[tuple(dim_slices)] = 0

def _restore_missing_value_zeros2(tensors, term_bin_weights):
    for tensor, weights in zip(tensors, term_bin_weights):
        n_dimensions = weights.ndim
        entire_tensor = [slice(None)] * n_dimensions
        lower = []
        higher = []
        for dimension_idx in range(n_dimensions):
            dim_slices = entire_tensor.copy()
            dim_slices[dimension_idx] = 0
            total_sum = np.sum(weights[tuple(dim_slices)])
            lower.append(True if total_sum == 0 else False)
            dim_slices[dimension_idx] = -1
            total_sum = np.sum(weights[tuple(dim_slices)])
            higher.append(True if total_sum == 0 else False)
        _zero_tensor(tensor, lower, higher)

def _weighted_std(a, axis, weights):
    average = np.average(a, axis , weights)
    variance = np.average((a - average)**2, axis , weights)
    return np.sqrt(variance)

def _convert_categorical_to_continuous(categories):
    # we do automagic detection of feature types by default, and sometimes a feature which
    # was really continuous might have most of it's data as one or two values.  An example would
    # be a feature that we have "0" and "1" in the training data, but "-0.1" and "3.1" are also
    # possible.  If during prediction we see a "3.1" we can magically convert our categories
    # into a continuous range with a cut point at 0.5.  Now "-0.1" goes into the [-inf, 0.5) bin
    # and 3.1 goes into the [0.5, +inf] bin.
    #
    # We can't convert a continuous feature that has cuts back into categoricals
    # since the categorical value could have been anything between the cuts that we know about.

    clusters = dict()
    non_float_idxs = set()

    old_min = np.nan
    old_max = np.nan
    for category, idx in categories.items():
        try:
            # this strips leading and trailing spaces
            val = float(category)
        except ValueError:
            non_float_idxs.add(idx)
            continue

        if isnan(val) or isinf(val):
            continue

        if isnan(old_min) or val < old_min:
            old_min = val
        if isnan(old_max) or old_max < val:
            old_max = val

        cluster_list = clusters.get(idx)
        if cluster_list is None:
            clusters[idx] = [val]
        else:
            cluster_list.append(val)

    # there's a super fringe case where two category strings map to the same bin, but 
    # one of them is a float and the other is a non-float.  Normally, we'd include the
    # non-float categorical in the unknowns, but in this case we'd need to include 
    # a part of a bin.  Handling this just adds too much complexity for the benefit
    # and you could argue that the evidence from the other models is indicating that
    # the string should be closer to zero of the weight from the floating point bin
    # so we take the simple route of putting all the weight into the float and none on the
    # non-float.  We still need to remove any indexes though that map to both a float
    # and a non-float, so this line handles that
    non_float_idxs = [idx for idx in non_float_idxs if idx not in clusters]
    non_float_idxs.append(max(categories.values()) + 1)

    if len(clusters) <= 1:
        return np.empty(0, np.float64)

    cluster_bounds = []
    for cluster_list in clusters.values():
        cluster_list.sort()
        cluster_bounds.append((cluster_list[0], cluster_list[-1]))

    # TODO: move everything below here into C++ to ensure cross language compatibility

    cluster_bounds.sort()

    cuts = []
    cluster_iter = iter(cluster_bounds)
    low = next(cluster_iter)[-1]
    for cluster in cluster_iter:
        high = cluster[0]
        if low < high:
            # if they are equal or if low is higher then we can't separate one cluster
            # from another, so we keep joining them until we can get clean separations

            half_diff = (high - low) / 2
            if isinf(half_diff):
                # first try to subtract then divide since that's more accurate but some float64
                # values will fail eg (max_float - min_float == +inf) so we need to try
                # a less accurate way of dividing first if we detect this.  Dividing
                # first will always succeed, even with the most extreme possible values of
                # max_float / 2 - min_float / 2
                half_diff = high / 2 - low / 2

            # floats have more precision the smaller they are, 
            # so use the smaller number as the anchor
            if abs(low) <= abs(high):
                mid = low + half_diff
            else:
                mid = high - half_diff

            if mid <= low:
                # this can happen with very small half_diffs that underflow the add/subtract operation
                # if this happens the numbers must be very close together on the order of a float tick.
                # We use lower bound inclusive for our cut discretization, so make the mid == high
                mid = high

            cuts.append(mid)
        low = max(low, cluster[-1])
    cuts = np.array(cuts, np.float64)

    mapping = [[] for _ in range(len(cuts) + 3)]
    for old_idx, cluster_list in clusters.items():
        # all the items in a cluster should be binned into the same bins
        new_idx = np.searchsorted(cuts, cluster_list[:1], side='right')[0] + 1
        mapping[new_idx].append(old_idx)

    mapping[0].append(0)
    mapping[-1] = non_float_idxs

    return cuts, mapping, old_min, old_max

def _create_proportional_tensor(axis_weights):
    # take the per-feature weights and distribute them proportionally to each cell in a tensor

    axis_sums = [weights.sum() for weights in axis_weights]

    # Normally you'd expect each axis to sum to the total weight from the model,
    # so normally they should be identical.  We encourage model editing though, so they may
    # not be identical under some edits.  Also, if the model is a DP model then the weights are
    # probably different due to the noise contribution.  Let's take the geometic mean to compensate.
    total_weight = exp(sum(log(axis_sum) for axis_sum in axis_sums) / len(axis_sums))
    axis_percentages = [weights / axis_sum for weights, axis_sum in zip(axis_weights, axis_sums)]

    shape = tuple(map(len, axis_percentages))
    n_cells = np.prod(shape)
    tensor = np.empty(n_cells, np.float64)

    # the last index items are next together in flat memory layout
    axis_percentages.reverse() 

    for cell_idx in range(n_cells):
        remainder = cell_idx
        frac = 1.0
        for percentages in axis_percentages:
            bin_idx = remainder % len(percentages)
            remainder //= len(percentages)
            frac *= percentages[bin_idx]
        val = frac * total_weight
        tensor.itemset(cell_idx, val)
    return tensor.reshape(shape)

def _process_terms(n_classes, n_samples, bagged_additive_terms, bin_weights, bag_weights):
    additive_terms = []
    term_standard_deviations = []
    for score_tensors in bagged_additive_terms:
        # TODO PK: shouldn't we be zero centering each score tensor first before taking the standard deviation
        # It's possible to shift scores arbitary to the intercept, so we should be able to get any desired stddev

        if (bag_weights == bag_weights[0]).all():
            # avoid numeracy issues if possible and ignore the weights if they are all equal
            additive_terms.append(np.average(score_tensors, axis=0))
            term_standard_deviations.append(np.std(score_tensors, axis=0))
        else:
            additive_terms.append(np.average(score_tensors, axis=0, weights=bag_weights))
            term_standard_deviations.append(_weighted_std(score_tensors, axis=0, weights=bag_weights))

    intercept = np.zeros(Native.get_count_scores_c(n_classes), np.float64)

    if n_classes <= 2:
        for idx in range(len(bagged_additive_terms)):
            score_mean = np.average(additive_terms[idx], weights=bin_weights[idx])
            additive_terms[idx] = (additive_terms[idx] - score_mean)

            # Add mean center adjustment back to intercept
            intercept += score_mean
    else:
        # Postprocess model graphs for multiclass
        multiclass_postprocess2(n_classes, n_samples, additive_terms, intercept, bin_weights)

    _restore_missing_value_zeros2(additive_terms, bin_weights)
    _restore_missing_value_zeros2(term_standard_deviations, bin_weights)

    if n_classes < 0:
        # scikit-learn uses a float for regression, and a numpy array with 1 element for binary classification
        intercept = float(intercept)

    return additive_terms, term_standard_deviations, intercept

def _generate_term_names(feature_names, term_features):
    return [" x ".join(feature_names[i] for i in grp) for grp in term_features]

def _generate_term_types(feature_types, term_features):
    return [feature_types[grp[0]] if len(grp) == 1 else "interaction" for grp in term_features]

def _order_terms(term_features, *args):
    keys = ([len(feature_idxs)] + sorted(feature_idxs) for feature_idxs in term_features)
    sorted_items = sorted(zip(keys, term_features, *args))
    ret = tuple(list(x) for x in islice(zip(*sorted_items), 1, None))
    # in Python if only 1 item exists then the item is returned and not a tuple
    return ret if 2 <= len(ret) else ret[0]

def _remove_unused_higher_bins(term_features, bins):
    # many features are not used in pairs, so we can simplify the model 
    # by removing the extra higher interaction level bins

    highest_levels = [0] * len(bins)
    for feature_idxs in term_features:
        for feature_idx in feature_idxs:
            highest_levels[feature_idx] = max(highest_levels[feature_idx], len(feature_idxs))

    for bin_levels, max_level in zip(bins, highest_levels):
        del bin_levels[max_level:]

def _deduplicate_bins(bins):
    # calling this function before calling score_terms allows score_terms to operate more efficiently since it'll
    # be able to avoid re-binning data for pairs that have already been processed in mains or other pairs since we 
    # use the id of the bins to identify feature data that was previously binned

    uniques = dict()
    for feature_idx in range(len(bins)):
        bin_levels = bins[feature_idx]
        highest_key = None
        for level_idx, feature_bins in enumerate(bin_levels):
            if isinstance(feature_bins, dict):
                key = frozenset(feature_bins.items())
            else:
                key = tuple(feature_bins)
            existing = uniques.get(key, None)
            if existing is None:
                uniques[key] = feature_bins
            else:
                bin_levels[level_idx] = existing

            if highest_key != key:
                highest_key = key
                highest_idx = level_idx
        del bin_levels[highest_idx + 1:]

def make_histogram_edges(min_val, max_val, histogram_counts):
    native = Native.get_native_singleton()
    cuts = native.cut_uniform(np.array([min_val, max_val], np.float64), len(histogram_counts) - 3)
    return np.concatenate(([min_val], cuts, [max_val]))

def _harmonize_tensor(
    new_feature_idxs, 
    new_bounds, 
    new_bins, 
    old_feature_idxs, 
    old_bounds, 
    old_bins, 
    old_mapping, 
    old_tensor, 
    bin_evidence_weight
):
    # TODO: don't pass in new_bound and old_bounds.  We use the bounds to proportion
    # weights at the tail ends of the graphs, but the problem with that is that
    # you can have outliers that'll stretch the weight very thin.  If you have an
    # old_min of -10000000 but the lowest old cut is at 0.  If you have a new cut
    # at -100, it'll put very very close to 0 weight in the region from -100 to 0.
    # Instead of using the min/max to proportionate, we should start from the
    # lowest new_bins cut and then find all the other models that have that exact
    # same lowest cut (averaging their results). There must be at least 1 model with that cut.  After we
    # find that other model, we can use the weights in existing bin_weights to 
    # proportionate the regions from the new lowest bin cut to the old lowest bin cut.
    # Do the same for the highest bin cut.  One issue is that the model(s) that have
    # the exact lowest bin cut are unlikely to share the lowest old cut, so we
    # proportionate the bin in the other model to the other model's next cut that is
    # greater than the old model's lowest cut.
    # eg:  new:      |    |            |   |    |
    #      old:                        |        |
    #   other1:      |    |   proprotion   |
    #   other2:      |        proportion        |
    # One wrinkle is that for pairs, we'll be using the pair cuts and we need to
    # one-dimensionalize any existing pair weights onto their respective 1D axies
    # before proportionating them.  Annother issue is that we might not even have
    # another term_feature that uses some particular feature that we use in our model
    # so we don't have any weights.  We can solve that issue by dropping any feature's
    # bins for terms that we have no information for.  After we do this we'll have
    # guaranteed that we only have new bin cuts for feature axies that we have inside
    # the bin level that we're handling!

    old_feature_idxs = list(old_feature_idxs)

    axes = []
    for feature_idx in new_feature_idxs:
        old_idx = old_feature_idxs.index(feature_idx)
        old_feature_idxs[old_idx] = -1 # in case we have duplicate feature idxs
        axes.append(old_idx)

    if len(axes) != old_tensor.ndim:
        # multiclass. The last dimension always stays put
        axes.append(len(axes))

    old_tensor = old_tensor.transpose(tuple(axes))
    if bin_evidence_weight is not None:
        bin_evidence_weight = bin_evidence_weight.transpose(tuple(axes))

    mapping = []
    lookups = []
    percentages = []
    for feature_idx in new_feature_idxs:
        old_bin_levels = old_bins[feature_idx]
        old_feature_bins = old_bin_levels[min(len(old_bin_levels), len(old_feature_idxs)) - 1]

        mapping_levels = old_mapping[feature_idx]
        old_feature_mapping = mapping_levels[min(len(mapping_levels), len(old_feature_idxs)) - 1]
        if old_feature_mapping is None:
            old_feature_mapping = list((x,) for x in range(len(old_feature_bins) + (2 if isinstance(old_feature_bins, dict) else 3)))
        mapping.append(old_feature_mapping)

        new_bin_levels = new_bins[feature_idx]
        new_feature_bins = new_bin_levels[min(len(new_bin_levels), len(new_feature_idxs)) - 1]

        if isinstance(new_feature_bins, dict):
            # categorical feature

            old_reversed = dict()
            for category, bin_idx in old_feature_bins.items():
                category_list = old_reversed.get(bin_idx)
                if category_list is None:
                    old_reversed[bin_idx] = [category]
                else:
                    category_list.append(category)

            new_reversed = dict()
            for category, bin_idx in new_feature_bins.items():
                category_list = new_reversed.get(bin_idx)
                if category_list is None:
                    new_reversed[bin_idx] = [category]
                else:
                    category_list.append(category)
            new_reversed = sorted(new_reversed.items())

            lookup = [0]
            percentage = [1.0]
            for _, new_categories in new_reversed:
                # if there are two items in new_categories then they should both resolve
                # to the same index in old_feature_bins otherwise they would have been
                # split into two categories
                old_bin_idx = old_feature_bins.get(new_categories[0], -1)
                if 0 <= old_bin_idx:
                    percentage.append(len(new_categories) / len(old_reversed[old_bin_idx]))
                else:
                    # map to the unknown bin for scores, but take no percentage of the weight
                    percentage.append(0.0)
                lookup.append(old_bin_idx)
            percentage.append(1.0)
            lookup.append(-1)
        else:
            # continuous feature

            lookup = list(np.searchsorted(old_feature_bins, new_feature_bins, side='left') + 1)
            lookup.append(len(old_feature_bins) + 1)

            percentage = [1.0]
            for new_idx_minus_one, old_idx in enumerate(lookup):
                if new_idx_minus_one == 0:
                    new_low = new_bounds[feature_idx, 0]
                    # TODO: if nan OR out of bounds from the cuts, estimate it.  If -inf or +inf, change it to min/max for float
                else:
                    new_low = new_feature_bins[new_idx_minus_one - 1]

                if len(new_feature_bins) <= new_idx_minus_one:
                    new_high = new_bounds[feature_idx, 1]
                    # TODO: if nan OR out of bounds from the cuts, estimate it.  If -inf or +inf, change it to min/max for float
                else:
                    new_high = new_feature_bins[new_idx_minus_one]


                if old_idx == 1:
                    old_low = old_bounds[feature_idx, 0]
                    # TODO: if nan OR out of bounds from the cuts, estimate it.  If -inf or +inf, change it to min/max for float
                else:
                    old_low = old_feature_bins[old_idx - 2]

                if len(old_feature_bins) < old_idx:
                    old_high = old_bounds[feature_idx, 1]
                    # TODO: if nan OR out of bounds from the cuts, estimate it.  If -inf or +inf, change it to min/max for float
                else:
                    old_high = old_feature_bins[old_idx - 1]

                if old_high <= new_low or new_high <= old_low:
                    # if there are bins in the area above where the old data extended, then 
                    # we'll have zero contribution in the old data where these new bins are
                    # located
                    percentage.append(0.0)
                else:
                    if new_low < old_low:
                        # this can't happen except at the lowest bin where the new min can be
                        # lower than the old min.  In that case we know the old data
                        # had zero contribution between the new min to the old min.
                        new_low = old_low

                    if old_high < new_high:
                        # this can't happen except at the lowest bin where the new max can be
                        # higher than the old max.  In that case we know the old data
                        # had zero contribution between the new max to the old max.
                        new_high = old_high

                    percentage.append((new_high - new_low) / (old_high - old_low))

            percentage.append(1.0)
            lookup.insert(0, 0)
            lookup.append(-1)

        lookups.append(lookup)
        percentages.append(percentage)

    new_shape = tuple(len(lookup) for lookup in lookups)
    n_cells = np.prod(new_shape)

    lookups.reverse()
    percentages.reverse()
    mapping.reverse()

    # now we need to inflate it
    new_tensor = np.empty(n_cells, np.float64)
    for cell_idx in range(n_cells):
        remainder = cell_idx
        old_reversed_bin_idxs = []
        frac = 1.0
        for lookup, percentage in zip(lookups, percentages):
            n_bins = len(lookup)
            new_bin_idx = remainder % n_bins
            remainder //= n_bins
            old_reversed_bin_idxs.append(lookup[new_bin_idx])
            frac *= percentage[new_bin_idx]

        cell_map = [map_bins[bin_idx] for map_bins, bin_idx in zip(mapping, old_reversed_bin_idxs)]
        n_cells2 = np.prod([len(x) for x in cell_map])
        val = 0.0
        total_weight = 0.0
        for cell2_idx in range(n_cells2):
            remainder2 = cell2_idx
            old_reversed_bin2_idxs = []
            for lookup2 in cell_map:
                n_bins2 = len(lookup2)
                new_bin2_idx = remainder2 % n_bins2
                remainder2 //= n_bins2
                old_reversed_bin2_idxs.append(lookup2[new_bin2_idx])
            update = old_tensor[tuple(reversed(old_reversed_bin2_idxs))]
            if n_cells2 == 1:
                # if there's just one cell, which is typical, don't 
                # incur the floating point loss in precision
                val = update
            else:
                if bin_evidence_weight is not None:
                    evidence_weight = bin_evidence_weight[tuple(reversed(old_reversed_bin2_idxs))]
                    update *= evidence_weight
                    total_weight += evidence_weight
                val += update
        if bin_evidence_weight is None:
            # we're doing a bin weight and NOT a score tensor
            val *= frac
        elif total_weight != 0.0:
            # we're doing scores and we need to take a weighted average
            # but if the total_weight is zero then val should be zero and
            # our update should still be zero, which it already is
            val = val / total_weight
        new_tensor.itemset(cell_idx, val)
    new_tensor = new_tensor.reshape(new_shape)
    return new_tensor

def merge_ebms(models):
    """ Merging multiple EBM models trained on the same dataset.
    Args:
        models: List of EBM models to be merged.
    Returns:
        An EBM model with averaged mean and standard deviation of input models.
    """

    if len(models) == 0:  # pragma: no cover
        raise Exception("0 models to merge.")

    model_types = list(set(map(type, models)))
    if len(model_types) == 2:
        type_names = [model_type.__name__ for model_type in model_types]
        if 'ExplainableBoostingClassifier' in type_names and 'DPExplainableBoostingClassifier' in type_names:
            ebm_type = model_types[type_names.index('ExplainableBoostingClassifier')]
            is_classifier = True
            is_private = False
        elif 'ExplainableBoostingRegressor' in type_names and 'DPExplainableBoostingRegressor' in type_names:
            ebm_type = model_types[type_names.index('ExplainableBoostingRegressor')]
            is_classifier = False
            is_private = False
        else:
            raise Exception("Inconsistent model types attempting to be merged.")
    elif len(model_types) == 1:
        ebm_type = model_types[0]
        if ebm_type.__name__ == 'ExplainableBoostingClassifier':
            is_classifier = True
            is_private = False
        elif ebm_type.__name__ == 'DPExplainableBoostingClassifier':
            is_classifier = True
            is_private = True
        elif ebm_type.__name__ == 'ExplainableBoostingRegressor':
            is_classifier = False
            is_private = False
        elif ebm_type.__name__ == 'DPExplainableBoostingRegressor':
            is_classifier = False
            is_private = True
        else:
            raise Exception(f"Invalid EBM model type {ebm_type.__name__} attempting to be merged.")
    else:
        raise Exception("Inconsistent model types being merged.")

    ebm = ebm_type.__new__(ebm_type)

    if any(not getattr(model, 'has_fitted_', False) for model in models):  # pragma: no cover
        raise Exception("All models must be fitted.")
    ebm.has_fitted_ = True

    # self.bins_ is the only feature based attribute that we absolutely require
    n_features = len(models[0].bins_)

    for model in models:
        if n_features != len(model.bins_):  # pragma: no cover
            raise Exception("Inconsistent numbers of features in the models.")

        feature_names_in = getattr(model, 'feature_names_in_', None)
        if feature_names_in is not None:
            if n_features != len(feature_names_in):  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

        feature_types_in = getattr(model, 'feature_types_in_', None)
        if feature_types_in is not None:
            if n_features != len(feature_types_in):  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

        feature_bounds = getattr(model, 'feature_bounds_', None)
        if feature_bounds is not None:
            if n_features != feature_bounds.shape[0]:  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

        histogram_counts = getattr(model, 'histogram_counts_', None)
        if histogram_counts is not None:
            if n_features != len(histogram_counts):  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

        unique_counts = getattr(model, 'unique_counts_', None)
        if unique_counts is not None:
            if n_features != len(unique_counts):  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

        zero_counts = getattr(model, 'zero_counts_', None)
        if zero_counts is not None:
            if n_features != len(zero_counts):  # pragma: no cover
                raise Exception("Inconsistent numbers of features in the models.")

    old_bounds = []
    old_mapping = []
    old_bins = []
    for model in models:
        if any(len(set(map(type, bin_levels))) != 1 for bin_levels in model.bins_):
            raise Exception("Inconsistent bin types within a model.")

        feature_bounds = getattr(model, 'feature_bounds_', None)
        if feature_bounds is None:
            old_bounds.append(None)
        else:
            old_bounds.append(feature_bounds.copy())

        old_mapping.append([[] for _ in range(n_features)])
        old_bins.append([[] for _ in range(n_features)])

    # TODO: every time we merge models we fragment the bins more and more and this is undesirable
    # especially for pairs.  When we build models, we store the feature bin cuts for pairs even
    # if we have no pairs that use that paritcular feature as a pair.  We can eliminate these useless
    # pair feature cuts before merging the bins and that'll give us less resulting cuts.  Having less
    # cuts reduces the number of estimates that we need to make and reduces the complexity of the
    # tensors, so it's good to have this reduction.
    
    new_feature_types = []
    new_bins = []
    for feature_idx in range(n_features):
        bin_types = set(type(model.bins_[feature_idx][0]) for model in models)

        if len(bin_types) == 1 and next(iter(bin_types)) is dict:
            # categorical
            new_feature_type = None
            for model in models:
                feature_types_in = getattr(model, 'feature_types_in_', None)
                if feature_types_in is not None:
                    feature_type = feature_types_in[feature_idx]
                    if feature_type == 'nominal':
                        new_feature_type = 'nominal'
                    elif feature_type == 'ordinal' and new_feature_type is None:
                        new_feature_type = 'ordinal'
            if new_feature_type is None:
                new_feature_type = 'nominal'
        else:
            # continuous
            if any(bin_type not in {dict, np.ndarray} for bin_type in bin_types):
                raise Exception("Invalid bin type.")
            new_feature_type = 'continuous'
        new_feature_types.append(new_feature_type)
            
        level_end = max(len(model.bins_[feature_idx]) for model in models)
        new_leveled_bins = []
        for level_idx in range(level_end):
            model_bins = []
            for model_idx, model in enumerate(models):
                bin_levels = model.bins_[feature_idx]
                bin_level = bin_levels[min(level_idx, len(bin_levels) - 1)]
                model_bins.append(bin_level)

                old_mapping[model_idx][feature_idx].append(None)
                old_bins[model_idx][feature_idx].append(bin_level)

            if len(bin_types) == 1 and next(iter(bin_types)) is dict:
                # categorical
                merged_keys = sorted(set(chain.from_iterable(bin.keys() for bin in model_bins)))
                # TODO: for now we just support alphabetical ordering in merged models, but
                # we could do all sort of special processing like trying to figure out if the original
                # ordering was by prevalence or alphabetical and then attempting to preserve that
                # order and also handling merged categories (where two categories map to a single score)
                # We should first try to progress in order along each set of keys and see if we can
                # establish the perfect order which might work if there are isolated missing categories
                # and if we can't get a unique guaranteed sorted order that way then examime all the
                # different known sort order and figure out if any of the possible orderings match
                merged_bins = dict(zip(merged_keys, count(1)))
            else:
                # continuous

                if 1 != len(bin_types):
                    # We have both categorical and continuous.  We can't convert continuous
                    # to categorical since we lack the original labels, but we can convert
                    # categoricals to continuous.  If the feature flavors are similar, which
                    # needs to be the case for model merging, one of the models only found
                    # float64 in their data, so there shouldn't be a lot of non-float values
                    # in the other models.

                    for model_idx, bins_in_model in enumerate(model_bins):
                        if isinstance(bins_in_model, dict):
                            converted_bins, mapping, converted_min, converted_max = _convert_categorical_to_continuous(bins_in_model)
                            model_bins[model_idx] = converted_bins

                            old_min = old_bounds[model_idx][feature_idx][0]
                            if isnan(old_min) or converted_min < old_min:
                                old_bounds[model_idx][feature_idx][0] = converted_min

                            old_max = old_bounds[model_idx][feature_idx][1]
                            if isnan(old_max) or old_max < converted_max:
                                old_bounds[model_idx][feature_idx][1] = converted_max

                            old_bins[model_idx][feature_idx][level_idx] = converted_bins
                            old_mapping[model_idx][feature_idx][level_idx] = mapping
                
                merged_bins = np.array(sorted(set(chain.from_iterable(model_bins))), np.float64)
            new_leveled_bins.append(merged_bins)
        new_bins.append(new_leveled_bins)
    ebm.feature_types_in_ = new_feature_types
    _deduplicate_bins(new_bins)
    ebm.bins_ = new_bins

    feature_names_merged = [None] * n_features
    for model in models:
        feature_names_in = getattr(model, 'feature_names_in_', None)
        if feature_names_in is not None:
            for feature_idx, feature_name in enumerate(feature_names_in):
                if feature_name is not None:
                    feature_name_merged = feature_names_merged[feature_idx]
                    if feature_name_merged is None:
                        feature_names_merged[feature_idx] = feature_name
                    elif feature_name != feature_name_merged:
                        raise Exception("All models should have the same feature names.")
    if any(feature_name is not None for feature_name in feature_names_merged):
        ebm.feature_names_in_ = feature_names_merged
    
    min_vals = [bounds[:, 0] for bounds in old_bounds if bounds is not None]
    max_vals = [bounds[:, 1] for bounds in old_bounds if bounds is not None]
    if 0 < len(min_vals): # max_vals has the same len
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)

            min_vals = np.nanmin(min_vals, axis=0)
            max_vals = np.nanmax(max_vals, axis=0)
            if any(not isnan(val) for val in min_vals) or any(not isnan(val) for val in max_vals):
                ebm.feature_bounds_ = np.array(list(zip(min_vals, max_vals)), np.float64)

    if not is_private:
        if all(hasattr(model, 'n_samples_') for model in models):
            ebm.n_samples_ = sum(model.n_samples_ for model in models)

        if all(hasattr(model, 'histogram_counts_') and hasattr(model, 'feature_bounds_') for model in models):
            if hasattr(ebm, 'feature_bounds_'):
                # TODO: estimate the histogram bin counts by taking the min of the mins and the max of the maxes
                # and re-apportioning the counts based on the distributions of the previous histograms.  Proprotion
                # them to the floor of their counts and then assign any remaining integers based on how much
                # they reduce the RMSE of the integer counts from the ideal floating point counts.
                pass

        if all(hasattr(model, 'zero_counts_') for model in models):
            ebm.zero_counts_ = np.sum([model.zero_counts_ for model in models], axis=0)

        if all(hasattr(model, 'bin_counts_') for model in models):
            # TODO: IF all models have bin counts then we should try and estimate the bin counts for
            # every term_feature, even though we won't have information on some of them.  At least we know
            # accurate bin counts.  If we're given a DP model then we shouldn't try and estimate them since
            # we didn't have it in the original model, so we should just use weights like in DP
            pass

    if is_classifier:
        ebm.classes_ = models[0].classes_.copy()
        if any(not np.array_equal(ebm.classes_, model.classes_) for model in models):  # pragma: no cover
            raise Exception("The target classes should be identical.")

        ebm._class_idx_ = {x: index for index, x in enumerate(ebm.classes_)}
        n_classes = len(ebm.classes_)
    else:
        if any(hasattr(model, 'min_target_') for model in models):
            ebm.min_target_ = min(model.min_target_ for model in models if hasattr(model, 'min_target_'))
        if any(hasattr(model, 'max_target_') for model in models):
            ebm.max_target_ = max(model.max_target_ for model in models if hasattr(model, 'max_target_'))
        n_classes = -1


    bag_weights = []
    model_weights = []
    for model in models:
        avg_weight = np.average([tensor.sum() for tensor in model.bin_weights_])
        model_weights.append(avg_weight)

        n_outer_bags = -1
        if hasattr(model, 'bagged_additive_terms_'):
            if 0 < len(model.bagged_additive_terms_):
                n_outer_bags = len(model.bagged_additive_terms_[0])

        model_bag_weights = getattr(model, 'bag_weights_', None)
        if model_bag_weights is None:
            # this model wasn't the result of a merge, so get the total weight for the model
            # every feature group in a model should have the same weight, but perhaps the user edited
            # the model weights and they don't agree.  We handle these by taking the average
            model_bag_weights = [avg_weight] * n_outer_bags
        elif len(model_bag_weights) != n_outer_bags:
            raise Exception("self.bagged_weights_ should have the same length as n_outer_bags.")

        bag_weights.extend(model_bag_weights)
    # this attribute wasn't available in the original model since we can calculate it for non-merged
    # models, but once a model is merged we need to preserve it for future merging or other uses
    # of the ebm.bagged_additive_terms_ attribute
    ebm.bag_weights_ = bag_weights

    fg_dicts = []
    all_fg = set()
    for model in models:
        fg_sorted = [tuple(sorted(feature_idxs)) for feature_idxs in model.term_features_]
        fg_dicts.append(dict(zip(fg_sorted, count(0))))
        all_fg.update(fg_sorted)

    sorted_fgs = _order_terms(list(all_fg))

    # TODO: in the future we might at this point try and figure out the most 
    #       common feature ordering within the feature groups.  Take the mode first
    #       and amonst the orderings that tie, choose the one that's best sorted by
    #       feature indexes
    ebm.term_features_ = sorted_fgs


    ebm.bin_weights_ = []
    ebm.bagged_additive_terms_ = []
    for sorted_fg in sorted_fgs:
        # since interactions are often automatically generated, we'll often always have 
        # interaction mismatches where an interaction will be in one model, but not the other.  
        # We need to estimate the bin_weight_ tensors that would have existed in this case.
        # We'll use the interaction terms that we do have in other models to estimate the 
        # distribution in the essense of the data, which should be roughly consistent or you
        # shouldn't be attempting to merge the models in the first place.  We'll then scale
        # the percentage distribution by the total weight of the model that we're fillin in the
        # details for.

        # TODO: this algorithm has some problems.  The estimated tensor that we get by taking the
        # model weight and distributing it by a per-cell percentage measure means that we get
        # inconsistent weight distibutions along the axis.  We can take our resulting weight tensor
        # and sum the columns/rows to get the weights on each individual feature axis.  Our model
        # however comes with a known set of weights on each feature, and the result of our operation
        # will not match the existing distribution in almost all cases.  I think there might be
        # some algorithm where we start with the per-feature weights and use the distribution hints
        # from the other models to inform where we place our exact weights that we know about in our
        # model from each axis.  The problem is that the sums in both axies need to agree, and each
        # change we make influences both.  I'm not sure we can even guarantee that there is an answer
        # and if there was one I'm not sure how we'd go about generating it.  I'm going to leave
        # this problem for YOU: a future person who is smarter than me and has more time to solve this.
        # One hint: I think a possible place to start would be an iterative algorithm that's similar
        # to purification where you randomly select a row/column and try to get closer at each step
        # to the rigth answer.  Good luck!
        #
        # Oh, there's also another deeper problem.. let's say you had a crazy 5 way interaction in the
        # model eg: (0,1,2,3,4) and you had 2 and 3 way interactions that either overlap or not.
        # Eg: (0,1), and either (1,2,3) or (2,3,4).  The ideal solution would take the 5 way interaction
        # and look for all the possible combinations of interactions for further information it could
        # use and then it would make something that is consistent across all of these disparate sources
        # of information.  Hopefully, the user hasn't edited the model in a way that creates no solution.

        bin_weight_percentages = []
        for model_idx, model, fg_dict, model_weight in zip(count(), models, fg_dicts, model_weights):
            term_idx = fg_dict.get(sorted_fg)
            if term_idx is not None:
                fixed_tensor = _harmonize_tensor(
                    sorted_fg,
                    ebm.feature_bounds_,
                    ebm.bins_, 
                    model.term_features_[term_idx], 
                    old_bounds[model_idx],
                    old_bins[model_idx],
                    old_mapping[model_idx],
                    model.bin_weights_[term_idx], 
                    None
                )
                bin_weight_percentages.append(fixed_tensor * model_weight)

        # use this when we don't have a feature group in a model as a reasonable 
        # set of guesses for the distribution of the weight of the model
        bin_weight_percentages = np.sum(bin_weight_percentages, axis=0)
        bin_weight_percentages = bin_weight_percentages / bin_weight_percentages.sum()

        additive_shape = bin_weight_percentages.shape
        if 2 < n_classes:
            additive_shape = tuple(list(additive_shape) + [n_classes])

        new_bin_weights = []
        new_bagged_additive_terms = []
        for model_idx, model, fg_dict, model_weight in zip(count(), models, fg_dicts, model_weights):
            n_outer_bags = -1
            if hasattr(model, 'bagged_additive_terms_'):
                if 0 < len(model.bagged_additive_terms_):
                    n_outer_bags = len(model.bagged_additive_terms_[0])

            term_idx = fg_dict.get(sorted_fg)
            if term_idx is None:
                new_bin_weights.append(model_weight * bin_weight_percentages)
                new_bagged_additive_terms.extend(n_outer_bags * [np.zeros(additive_shape, np.float64)])
            else:
                harmonized_bin_weights = _harmonize_tensor(
                    sorted_fg,
                    ebm.feature_bounds_,
                    ebm.bins_, 
                    model.term_features_[term_idx], 
                    old_bounds[model_idx],
                    old_bins[model_idx],
                    old_mapping[model_idx],
                    model.bin_weights_[term_idx], 
                    None
                )
                new_bin_weights.append(harmonized_bin_weights)
                for bag_idx in range(n_outer_bags):
                    harmonized_bagged_additive_terms = _harmonize_tensor(
                        sorted_fg,
                        ebm.feature_bounds_,
                        ebm.bins_, 
                        model.term_features_[term_idx], 
                        old_bounds[model_idx],
                        old_bins[model_idx],
                        old_mapping[model_idx],
                        model.bagged_additive_terms_[term_idx][bag_idx], 
                        model.bin_weights_[term_idx] # we use these to weigh distribution of scores for mulple bins
                    )
                    new_bagged_additive_terms.append(harmonized_bagged_additive_terms)
        ebm.bin_weights_.append(np.sum(new_bin_weights, axis=0))
        ebm.bagged_additive_terms_.append(np.array(new_bagged_additive_terms, np.float64))

    ebm.additive_terms_, ebm.term_standard_deviations_, ebm.intercept_ = _process_terms(
        n_classes, 
        ebm.n_samples_, 
        ebm.bagged_additive_terms_, 
        ebm.bin_weights_,
        ebm.bag_weights_
    )


    # TODO: we might be able to do these operations earlier
    _remove_unused_higher_bins(ebm.term_features_, ebm.bins_)
    # removing the higher order terms might allow us to eliminate some extra bins now that couldn't before
    _deduplicate_bins(ebm.bins_)


    # dependent attributes (can be re-derrived after serialization)
    ebm.n_features_in_ = len(ebm.bins_) # scikit-learn specified name
    ebm.term_names_ = _generate_term_names(ebm.feature_names_in_, ebm.term_features_)

    return ebm

# TODO: Clean up
class EBMUtils:
    
    @staticmethod
    def normalize_initial_random_seed(seed):  # pragma: no cover
        # Some languages do not support 64-bit values.  Other languages do not support unsigned integers.
        # Almost all languages support signed 32-bit integers, so we standardize on that for our 
        # random number seed values.  If the caller passes us a number that doesn't fit into a 
        # 32-bit signed integer, we convert it.  This conversion doesn't need to generate completely 
        # uniform results provided they are reasonably uniform, since this is just the seed.
        # 
        # We use a simple conversion because we use the same method in multiple languages, 
        # and we need to keep the results identical between them, so simplicity is key.
        # 
        # The result of the modulo operator is not standardized accross languages for 
        # negative numbers, so take the negative before the modulo if the number is negative.
        # https://torstencurdt.com/tech/posts/modulo-of-negative-numbers

        if 2147483647 <= seed:
            return seed % 2147483647
        if seed <= -2147483647:
            return -((-seed) % 2147483647)
        return seed

    # NOTE: Interval / cut conversions are future work. Not registered for code coverage.
    @staticmethod
    def convert_to_intervals(cuts):  # pragma: no cover
        cuts = np.array(cuts, dtype=np.float64)

        if np.isnan(cuts).any():
            raise Exception("cuts cannot contain nan")

        if np.isinf(cuts).any():
            raise Exception("cuts cannot contain infinity")

        smaller = np.insert(cuts, 0, -np.inf)
        larger = np.append(cuts, np.inf)
        intervals = list(zip(smaller, larger))

        if any(x[1] <= x[0] for x in intervals):
            raise Exception("cuts must contain increasing values")

        return intervals

    @staticmethod
    def convert_to_cuts(intervals):  # pragma: no cover
        if len(intervals) == 0:
            raise Exception("intervals must have at least one interval")

        if any(len(x) != 2 for x in intervals):
            raise Exception("intervals must be a list of tuples")

        if intervals[0][0] != -np.inf:
            raise Exception("intervals must start from -inf")

        if intervals[-1][-1] != np.inf:
            raise Exception("intervals must end with inf")

        cuts = [x[0] for x in intervals[1:]]
        cuts_verify = [x[1] for x in intervals[:-1]]

        if np.isnan(cuts).any():
            raise Exception("intervals cannot contain NaN")

        if any(x[0] != x[1] for x in zip(cuts, cuts_verify)):
            raise Exception("intervals must contain adjacent sections")

        if any(higher <= lower for lower, higher in zip(cuts, cuts[1:])):
            raise Exception("intervals must contain increasing sections")

        return cuts

    @staticmethod
    def make_bag(y, test_size, random_state, is_classification):
        # all test/train splits should be done with this function to ensure that
        # if we re-generate the train/test splits that they are generated exactly
        # the same as before

        if test_size == 0:
            return None
        elif test_size > 0:
            n_samples = len(y)
            n_test_samples = 0

            if test_size >= 1:
                if test_size % 1:
                    raise Exception("If test_size >= 1, test_size should be a whole number.")
                n_test_samples = test_size 
            else:
                n_test_samples = ceil(n_samples * test_size)

            n_train_samples = n_samples - n_test_samples
            native = Native.get_native_singleton()

            # Adapt test size if too small relative to number of classes
            if is_classification:
                y_uniq = len(set(y))
                if n_test_samples < y_uniq:  # pragma: no cover
                    warnings.warn(
                        "Too few samples per class, adapting test size to guarantee 1 sample per class."
                    )
                    n_test_samples = y_uniq
                    n_train_samples = n_samples - n_test_samples

                return native.stratified_sampling_without_replacement(
                    random_state,
                    y_uniq,
                    n_train_samples,
                    n_test_samples,
                    y
                )
            else:
                return native.sample_without_replacement(
                    random_state,
                    n_train_samples,
                    n_test_samples
                )
        else:  # pragma: no cover
            raise Exception("test_size must be a positive numeric value.")

    @staticmethod
    def jsonify_lists(vals):
        if len(vals) != 0:
            if type(vals[0]) is float:
                for idx, val in enumerate(vals):
                    # JSON doesn't have NaN, or infinities, but javaScript has these, so use javaScript strings
                    if isnan(val):
                        vals[idx] = "NaN" # this is what JavaScript outputs for 0/0
                    elif val == np.inf:
                        vals[idx] = "Infinity" # this is what JavaScript outputs for 1/0
                    elif val == -np.inf:
                        vals[idx] = "-Infinity" # this is what JavaScript outputs for -1/0
            else:
                for nested in vals:
                    EBMUtils.jsonify_lists(nested)
        return vals # we modify in place, but return it just for easy access

    @staticmethod
    def jsonify_item(val):
        # JSON doesn't have NaN, or infinities, but javaScript has these, so use javaScript strings
        if isnan(val):
            val = "NaN" # this is what JavaScript outputs for 0/0
        elif val == np.inf:
            val = "Infinity" # this is what JavaScript outputs for 1/0
        elif val == -np.inf:
            val = "-Infinity" # this is what JavaScript outputs for -1/0
        return val

    @staticmethod
    def cyclic_gradient_boost(
        dataset,
        bag,
        scores,
        term_features,
        n_inner_bags,
        boosting_flags,
        learning_rate,
        min_samples_leaf,
        max_leaves,
        early_stopping_rounds,
        early_stopping_tolerance,
        max_rounds,
        noise_scale,
        bin_weights,
        random_state,
        optional_temp_params=None,
    ):
        min_metric = np.inf
        episode_index = 0
        with Booster(
            dataset,
            bag,
            scores,
            term_features,
            n_inner_bags,
            random_state,
            optional_temp_params,
        ) as booster:
            no_change_run_length = 0
            bp_metric = np.inf
            _log.info("Start boosting")
            for episode_index in range(max_rounds):
                if episode_index % 10 == 0:
                    _log.debug("Sweep Index {0}".format(episode_index))
                    _log.debug("Metric: {0}".format(min_metric))

                for term_idx in range(len(term_features)):
                    avg_gain = booster.generate_term_update(
                        term_idx=term_idx,
                        boosting_flags=boosting_flags,
                        learning_rate=learning_rate,
                        min_samples_leaf=min_samples_leaf,
                        max_leaves=max_leaves,
                    )

                    if noise_scale: # Differentially private updates
                        splits = booster.get_term_update_splits()[0]

                        term_update_tensor = booster.get_term_update_expanded()
                        noisy_update_tensor = term_update_tensor.copy()

                        splits_iter = [0] + list(splits + 1) + [len(term_update_tensor)] # Make splits iteration friendly
                        # Loop through all random splits and add noise before updating
                        for f, s in zip(splits_iter[:-1], splits_iter[1:]):
                            if s == 1: 
                                continue # Skip cuts that fall on 0th (missing value) bin -- missing values not supported in DP

                            noise = np.random.normal(0.0, noise_scale)
                            noisy_update_tensor[f:s] = term_update_tensor[f:s] + noise

                            # Native code will be returning sums of residuals in slices, not averages.
                            # Compute noisy average by dividing noisy sum by noisy bin weights
                            instance_weight = np.sum(bin_weights[term_idx][f:s])
                            noisy_update_tensor[f:s] = noisy_update_tensor[f:s] / instance_weight

                        noisy_update_tensor = noisy_update_tensor * -1 # Invert gradients before updates
                        booster.set_term_update_expanded(term_idx, noisy_update_tensor)


                    curr_metric = booster.apply_term_update()

                    min_metric = min(curr_metric, min_metric)

                # TODO PK this early_stopping_tolerance is a little inconsistent
                #      since it triggers intermittently and only re-triggers if the
                #      threshold is re-passed, but not based on a smooth windowed set
                #      of checks.  We can do better by keeping a list of the last
                #      number of measurements to have a consistent window of values.
                #      If we only cared about the metric at the start and end of the epoch
                #      window a circular buffer would be best choice with O(1).
                if no_change_run_length == 0:
                    bp_metric = min_metric
                if min_metric + early_stopping_tolerance < bp_metric:
                    no_change_run_length = 0
                else:
                    no_change_run_length += 1

                if (
                    early_stopping_rounds >= 0
                    and no_change_run_length >= early_stopping_rounds
                ):
                    break

            _log.info(
                "End boosting, Best Metric: {0}, Num Rounds: {1}".format(
                    min_metric, episode_index
                )
            )

            # TODO: Add more ways to call alternative get_current_model
            # Use latest model if there are no instances in the (transposed) validation set 
            # or if training with privacy
            if bag is None or noise_scale is not None:
                model_update = booster.get_current_model()
            else:
                model_update = booster.get_best_model()

        return model_update, episode_index

    @staticmethod
    def calc_interaction_order(
        dataset,
        bag,
        scores,
        iter_term_features,
        interaction_options, 
        min_samples_leaf,
        optional_temp_params=None,
    ):
        interaction_strengths = []
        with InteractionDetector(dataset, bag, scores, optional_temp_params) as interaction_detector:
            for feature_idxs in iter_term_features:
                strength = interaction_detector.calc_interaction_strength(
                    feature_idxs, interaction_options, min_samples_leaf,
                )
                interaction_strengths.append((strength, feature_idxs))

        interaction_strengths.sort(reverse=True)
        return list(map(operator.itemgetter(1), interaction_strengths))


class DPUtils:

    @staticmethod
    def calc_classic_noise_multi(total_queries, target_epsilon, delta, sensitivity):
        variance = (8*total_queries*sensitivity**2 * np.log(np.exp(1) + target_epsilon / delta)) / target_epsilon ** 2
        return np.sqrt(variance)

    @staticmethod
    def calc_gdp_noise_multi(total_queries, target_epsilon, delta):
        ''' GDP analysis following Algorithm 2 in: https://arxiv.org/abs/2106.09680. 
        '''
        def f(mu, eps, delta):
            return DPUtils.delta_eps_mu(eps, mu) - delta

        final_mu = brentq(lambda x: f(x, target_epsilon, delta), 1e-5, 1000)
        sigma = np.sqrt(total_queries) / final_mu
        return sigma

    # General calculations, largely borrowed from tensorflow/privacy and presented in https://arxiv.org/abs/1911.11607
    @staticmethod
    def delta_eps_mu(eps, mu):
        ''' Code adapted from: https://github.com/tensorflow/privacy/blob/master/tensorflow_privacy/privacy/analysis/gdp_accountant.py#L44
        '''
        return norm.cdf(-eps/mu + mu/2) - np.exp(eps) * norm.cdf(-eps/mu - mu/2)

    @staticmethod
    def eps_from_mu(mu, delta):
        ''' Code adapted from: https://github.com/tensorflow/privacy/blob/master/tensorflow_privacy/privacy/analysis/gdp_accountant.py#L50
        '''
        def f(x):
            return DPUtils.delta_eps_mu(x, mu)-delta    
        return root_scalar(f, bracket=[0, 500], method='brentq').root

    @staticmethod
    def private_numeric_binning(col_data, sample_weight, noise_scale, max_bins, min_val, max_val):
        uniform_weights, uniform_edges = np.histogram(col_data, bins=max_bins*2, range=(min_val, max_val), weights=sample_weight)
        noisy_weights = uniform_weights + np.random.normal(0, noise_scale, size=uniform_weights.shape[0])
        
        # Postprocess to ensure realistic bin values (min=0)
        noisy_weights = np.clip(noisy_weights, 0, None)

        # TODO PK: check with Harsha, but we can probably alternate the taking of nibbles from both ends
        # so that the larger leftover bin tends to be in the center rather than on the right.

        # Greedily collapse bins until they meet or exceed target_weight threshold
        sample_weight_total = len(col_data) if sample_weight is None else np.sum(sample_weight)
        target_weight = sample_weight_total / max_bins
        bin_weights, bin_cuts = [0], [uniform_edges[0]]
        curr_weight = 0
        for index, right_edge in enumerate(uniform_edges[1:]):
            curr_weight += noisy_weights[index]
            if curr_weight >= target_weight:
                bin_cuts.append(right_edge)
                bin_weights.append(curr_weight)
                curr_weight = 0

        if len(bin_weights) == 1:
            # since we're adding unbounded random noise, it's possible that the total weight is less than the
            # threshold required for a single bin.  It could in theory even be negative.
            # clip to the target_weight.  If we had more than the target weight we'd have a bin

            bin_weights.append(target_weight)
            bin_cuts = np.empty(0, dtype=np.float64)
        else:
            # Ignore min/max value as part of cut definition
            bin_cuts = np.array(bin_cuts, dtype=np.float64)[1:-1]

            # All leftover datapoints get collapsed into final bin
            bin_weights[-1] += curr_weight

        return bin_cuts, bin_weights

    @staticmethod
    def private_categorical_binning(col_data, sample_weight, noise_scale, max_bins):
        # Initialize estimate
        col_data = col_data.astype('U')
        uniq_vals, uniq_idxs = np.unique(col_data, return_inverse=True)
        weights = np.bincount(uniq_idxs, weights=sample_weight, minlength=len(uniq_vals))

        weights = weights + np.random.normal(0, noise_scale, size=weights.shape[0])

        # Postprocess to ensure realistic bin values (min=0)
        weights = np.clip(weights, 0, None)

        # Collapse bins until target_weight is achieved.
        sample_weight_total = len(col_data) if sample_weight is None else np.sum(sample_weight)
        target_weight = sample_weight_total / max_bins
        small_bins = np.where(weights < target_weight)[0]
        if len(small_bins) > 0:
            other_weight = np.sum(weights[small_bins])
            mask = np.ones(weights.shape, dtype=bool)
            mask[small_bins] = False

            # Collapse all small bins into "DPOther"
            uniq_vals = np.append(uniq_vals[mask], "DPOther")
            weights = np.append(weights[mask], other_weight)

            if other_weight < target_weight:
                if len(weights) == 1:
                    # since we're adding unbounded random noise, it's possible that the total weight is less than the
                    # threshold required for a single bin.  It could in theory even be negative.
                    # clip to the target_weight
                    weights[0] = target_weight
                else:
                    # If "DPOther" bin is too small, absorb 1 more bin (guaranteed above threshold)
                    collapse_bin = np.argmin(weights[:-1])
                    mask = np.ones(weights.shape, dtype=bool)
                    mask[collapse_bin] = False

                    # Pack data into the final "DPOther" bin
                    weights[-1] += weights[collapse_bin]

                    # Delete absorbed bin
                    uniq_vals = uniq_vals[mask]
                    weights = weights[mask]

        return uniq_vals, weights

    @staticmethod
    def validate_eps_delta(eps, delta):
        if eps is None or eps <= 0 or delta is None or delta <= 0:
            raise ValueError(f"Epsilon: '{eps}' and delta: '{delta}' must be set to positive numbers")
