# Copyright (c) 2019 Microsoft Corporation
# Distributed under the MIT software license


from typing import DefaultDict

from interpret.provider.visualize import PreserveProvider
from ...utils import gen_perf_dicts
from .utils import DPUtils, EBMUtils
from .bin import clean_X, clean_vector, construct_bins, bin_python, ebm_decision_function, ebm_decision_function_and_explain, make_boosting_counts, restore_missing_value_zeros, restore_missing_value_zeros2, after_boosting, remove_last, remove_last2, get_counts_and_weights, trim_tensor, unify_data2, eval_terms
from .internal import Native
from .postprocessing import multiclass_postprocess2
from ...utils import unify_data, autogen_schema, unify_vector
from ...api.base import ExplainerMixin
from ...api.templates import FeatureValueExplanation
from ...provider.compute import JobLibProvider
from ...utils import gen_name_from_class, gen_global_selector, gen_global_selector2, gen_local_selector
import ctypes as ct
from multiprocessing.sharedctypes import RawArray

import numpy as np
from warnings import warn

from sklearn.base import is_classifier
from sklearn.utils.validation import check_is_fitted
from sklearn.metrics import log_loss, mean_squared_error
import heapq

from sklearn.base import (
    BaseEstimator,
    TransformerMixin,
    ClassifierMixin,
    RegressorMixin,
)
from sklearn.utils.extmath import softmax
from itertools import combinations

import logging

_log = logging.getLogger(__name__)


class EBMExplanation(FeatureValueExplanation):
    """ Visualizes specifically for EBM. """

    explanation_type = None

    def __init__(
        self,
        explanation_type,
        internal_obj,
        feature_names=None,
        feature_types=None,
        name=None,
        selector=None,
    ):
        """ Initializes class.

        Args:
            explanation_type:  Type of explanation.
            internal_obj: A jsonable object that backs the explanation.
            feature_names: List of feature names.
            feature_types: List of feature types.
            name: User-defined name of explanation.
            selector: A dataframe whose indices correspond to explanation entries.
        """
        super(EBMExplanation, self).__init__(
            explanation_type,
            internal_obj,
            feature_names=feature_names,
            feature_types=feature_types,
            name=name,
            selector=selector,
        )

    def visualize(self, key=None):
        """ Provides interactive visualizations.

        Args:
            key: Either a scalar or list
                that indexes the internal object for sub-plotting.
                If an overall visualization is requested, pass None.

        Returns:
            A Plotly figure.
        """
        from ...visual.plot import (
            plot_continuous_bar,
            plot_horizontal_bar,
            sort_take,
            is_multiclass_global_data_dict,
        )

        data_dict = self.data(key)
        if data_dict is None:
            return None

        # Overall graph
        if self.explanation_type == "global" and key is None:
            data_dict = sort_take(
                data_dict, sort_fn=lambda x: -abs(x), top_n=15, reverse_results=True
            )
            figure = plot_horizontal_bar(
                data_dict,
                title="Overall Importance:<br>Mean Absolute Score",
                start_zero=True,
            )

            return figure

        # Continuous feature graph
        if (
            self.explanation_type == "global"
            and self.feature_types[key] == "continuous"
        ):
            title = self.feature_names[key]
            if is_multiclass_global_data_dict(data_dict):
                figure = plot_continuous_bar(
                    data_dict, multiclass=True, show_error=False, title=title
                )
            else:
                figure = plot_continuous_bar(data_dict, title=title)

            return figure

        return super().visualize(key)


# TODO: More documentation in binning process to be explicit.
# TODO: Consider stripping this down to the bare minimum.
class EBMPreprocessor(BaseEstimator, TransformerMixin):
    """ Transformer that preprocesses data to be ready before EBM. """

    def __init__(
        self, feature_names=None, feature_types=None, max_bins=256, binning="quantile", missing_str=str(np.nan), 
        epsilon=None, delta=None, privacy_schema=None
    ):
        """ Initializes EBM preprocessor.

        Args:
            feature_names: Feature names as list.
            feature_types: Feature types as list, for example "continuous" or "categorical".
            max_bins: Max number of bins to process numeric features.
            binning: Strategy to compute bins: "quantile", "quantile_humanized", "uniform", or "private". 
            missing_str: By default np.nan values are missing for all datatypes. Setting this parameter changes the string representation for missing
            epsilon: Privacy budget parameter. Only applicable when binning is "private".
            delta: Privacy budget parameter. Only applicable when binning is "private".
            privacy_schema: User specified min/maxes for numeric features as dictionary. Only applicable when binning is "private".
        """
        self.feature_names = feature_names
        self.feature_types = feature_types
        self.max_bins = max_bins
        self.binning = binning
        self.missing_str = missing_str
        self.epsilon = epsilon
        self.delta = delta
        self.privacy_schema = privacy_schema

    def fit(self, X):
        """ Fits transformer to provided samples.

        Args:
            X: Numpy array for training samples.

        Returns:
            Itself.
        """

        self.col_bin_edges_ = {}
        self.col_min_ = {}
        self.col_max_ = {}

        self.hist_counts_ = {}
        self.hist_edges_ = {}

        self.col_mapping_ = {}

        self.col_bin_counts_ = []
        self.col_names_ = []
        self.col_types_ = []

        self.has_fitted_ = False

        native = Native.get_native_singleton()
        schema = autogen_schema(
            X, feature_names=self.feature_names, feature_types=self.feature_types
        )

        noise_scale = None # only applicable for private binning
        if "private" in self.binning:
            DPUtils.validate_eps_delta(self.epsilon, self.delta)
            noise_scale = DPUtils.calc_gdp_noise_multi(
                total_queries = X.shape[1], 
                target_epsilon = self.epsilon, 
                delta = self.delta
            )
            if self.privacy_schema is None:
                warn("Possible privacy violation: assuming min/max values per feature are public info."
                     "Pass a privacy schema with known public ranges per feature to avoid this warning.")
                self.privacy_schema = DPUtils.build_privacy_schema(X)
                
        if self.max_bins < 2:
            raise ValueError("max_bins must be 2 or higher.  One bin is required for missing, and annother for non-missing values.")

        for col_idx in range(X.shape[1]):
            col_name = list(schema.keys())[col_idx]
            self.col_names_.append(col_name)

            col_info = schema[col_name]
            assert col_info["column_number"] == col_idx
            col_data = X[:, col_idx]

            self.col_types_.append(col_info["type"])
            if col_info["type"] == "continuous":
                col_data = col_data.astype(float)
                if self.binning == "private":
                    min_val, max_val = self.privacy_schema[col_idx]
                    cuts, bin_counts = DPUtils.private_numeric_binning(
                        col_data, noise_scale, self.max_bins, min_val, max_val
                    )

                    # Use previously calculated bins for density estimates
                    hist_edges = np.concatenate([[min_val], cuts, [max_val]])
                    hist_counts = bin_counts[1:]
                else:  # Standard binning
                    min_samples_bin = 1 # TODO: Expose
                    is_humanized = 0
                    if self.binning == 'quantile' or self.binning == 'quantile_humanized':
                        if self.binning == 'quantile_humanized':
                            is_humanized = 1

                        # one bin for missing, and # of cuts is one less again
                        cuts = native.cut_quantile(col_data, min_samples_bin, is_humanized, self.max_bins - 2)
                    elif self.binning == "uniform":
                        # one bin for missing, and # of cuts is one less again
                        cuts = native.cut_uniform(col_data, self.max_bins - 2)
                    else:
                        raise ValueError(f"Unrecognized bin type: {self.binning}")

                    min_val = np.nanmin(col_data)
                    max_val = np.nanmax(col_data)

                    discretized = native.discretize(col_data, cuts)
                    bin_counts = np.bincount(discretized, minlength=len(cuts) + 2)
                    col_data = col_data[~np.isnan(col_data)]

                    hist_counts, hist_edges = np.histogram(col_data, bins="doane")

                
                self.col_bin_counts_.append(bin_counts)
                self.col_bin_edges_[col_idx] = cuts
                self.col_min_[col_idx] = min_val
                self.col_max_[col_idx] = max_val
                self.hist_edges_[col_idx] = hist_edges
                self.hist_counts_[col_idx] = hist_counts
            elif col_info["type"] == "ordinal":
                mapping = {val: indx + 1 for indx, val in enumerate(col_info["order"])}
                self.col_mapping_[col_idx] = mapping
                self.col_bin_counts_.append(None) # TODO count the values in each bin
            elif col_info["type"] == "categorical":
                col_data = col_data.astype('U')

                if self.binning == "private":
                    uniq_vals, counts = DPUtils.private_categorical_binning(col_data, noise_scale, self.max_bins)
                else: # Standard binning
                    uniq_vals, counts = np.unique(col_data, return_counts=True)

                missings = np.isin(uniq_vals, self.missing_str)

                count_missing = np.sum(counts[missings])
                bin_counts = np.concatenate(([count_missing], counts[~missings]))
                self.col_bin_counts_.append(bin_counts)

                uniq_vals = uniq_vals[~missings]
                mapping = {val: indx + 1 for indx, val in enumerate(uniq_vals)}
                self.col_mapping_[col_idx] = mapping

        self.has_fitted_ = True
        return self

    def transform(self, X):
        """ Transform on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Transformed numpy array.
        """
        check_is_fitted(self, "has_fitted_")

        missing_constant = 0
        unknown_constant = -1

        native = Native.get_native_singleton()

        X_new = np.copy(X)
        if issubclass(X.dtype.type, np.unsignedinteger):
            X_new = X_new.astype(np.int64)

        for col_idx in range(X.shape[1]):
            col_type = self.col_types_[col_idx]
            col_data = X[:, col_idx]

            if col_type == "continuous":
                col_data = col_data.astype(float)
                cuts = self.col_bin_edges_[col_idx]

                discretized = native.discretize(col_data, cuts)
                X_new[:, col_idx] = discretized

            elif col_type == "ordinal":
                mapping = self.col_mapping_[col_idx].copy()
                vec_map = np.vectorize(
                    lambda x: mapping[x] if x in mapping else unknown_constant
                )
                X_new[:, col_idx] = vec_map(col_data)
            elif col_type == "categorical":
                mapping = self.col_mapping_[col_idx].copy()

                # Use "DPOther" bin when possible to handle unknown values during DP.
                if "private" in self.binning:
                    for key, val in mapping.items():
                        if key == "DPOther": 
                            unknown_constant = val
                            missing_constant = val
                            break
                    else: # If DPOther keyword doesn't exist, revert to standard encoding scheme
                        missing_constant = 0
                        unknown_constant = -1

                if isinstance(self.missing_str, list):
                    for val in self.missing_str:
                        mapping[val] = missing_constant
                else:
                    mapping[self.missing_str] = missing_constant

                col_data = col_data.astype('U')
                X_new[:, col_idx] = np.fromiter(
                    (mapping.get(x, unknown_constant) for x in col_data), dtype=np.int64, count=X.shape[0]
                )

        return X_new.astype(np.int64)

    def _get_hist_counts(self, feature_index):
        col_type = self.col_types_[feature_index]
        if col_type == "continuous":
            return list(self.hist_counts_[feature_index])
        elif col_type == "categorical":
            return list(self.col_bin_counts_[feature_index][1:])
        else:  # pragma: no cover
            raise Exception("Cannot get counts for type: {0}".format(col_type))

    def _get_hist_edges(self, feature_index):
        col_type = self.col_types_[feature_index]
        if col_type == "continuous":
            return list(self.hist_edges_[feature_index])
        elif col_type == "categorical":
            map = self.col_mapping_[feature_index]
            return list(map.keys())
        else:  # pragma: no cover
            raise Exception("Cannot get counts for type: {0}".format(col_type))


    def _get_bin_labels(self, feature_index):
        """ Returns bin labels for a given feature index.

        Args:
            feature_index: An integer for feature index.

        Returns:
            List of labels for bins.
        """

        col_type = self.col_types_[feature_index]
        if col_type == "continuous":
            min_val = self.col_min_[feature_index]
            cuts = self.col_bin_edges_[feature_index]
            max_val = self.col_max_[feature_index]
            return list(np.concatenate(([min_val], cuts, [max_val])))
        elif col_type == "ordinal":
            map = self.col_mapping_[feature_index]
            return list(map.keys())
        elif col_type == "categorical":
            map = self.col_mapping_[feature_index]
            return list(map.keys())
        else:  # pragma: no cover
            raise Exception("Unknown column type")

def _parallel_cyclic_gradient_boost(
    scores_train,
    scores_val,
    X, 
    y, 
    w, 
    feature_indices,
    n_classes,
    validation_size,
    model_type,
    update,
    features_categorical,
    features_bin_count,
    inner_bags,
    learning_rate,
    min_samples_leaf,
    max_leaves,
    early_stopping_rounds,
    early_stopping_tolerance,
    max_rounds,
    random_state,
    noise_scale,
    bin_counts,
):
    _log.info("Splitting train/test")

    X_train, X_val, y_train, y_val, w_train, w_val, _, _ = EBMUtils.ebm_train_test_split(
        X,
        y,
        w,
        test_size=validation_size,
        random_state=random_state,
        is_classification=model_type == "classification",
    )

    _log.info("Cyclic boost")

    (
        model_update,
        current_metric,
        episode_idx,
    ) = EBMUtils.cyclic_gradient_boost(
        model_type=model_type,
        n_classes=n_classes,
        features_categorical = features_categorical, 
        features_bin_count = features_bin_count, 
        feature_groups=feature_indices,
        X_train=X_train,
        y_train=y_train,
        w_train=w_train,
        scores_train=scores_train,
        X_val=X_val,
        y_val=y_val,
        w_val=w_val,
        scores_val=scores_val,
        n_inner_bags=inner_bags,
        generate_update_options=update,
        learning_rate=learning_rate,
        min_samples_leaf=min_samples_leaf,
        max_leaves=max_leaves,
        early_stopping_rounds=early_stopping_rounds,
        early_stopping_tolerance=early_stopping_tolerance,
        max_rounds=max_rounds,
        random_state=random_state,
        name="Boost",
        noise_scale=noise_scale,
        bin_counts=bin_counts,
    )
    return model_update, episode_idx

def _parallel_get_interactions(
    scores_train,
    X, 
    y, 
    w, 
    n_classes,
    validation_size,
    random_state,
    model_type,
    features_categorical, 
    features_bin_count, 
    min_samples_leaf,
):
    _log.info("Splitting train/test")

    X_train, _, y_train, _, w_train, _, _, _ = EBMUtils.ebm_train_test_split(
        X,
        y,
        w,
        test_size=validation_size,
        random_state=random_state,
        is_classification=model_type == "classification",
    )
        
    _log.info("Estimating with FAST")

    iter_feature_groups = combinations(range(X.shape[1]), 2)

    final_indices, final_scores = EBMUtils.get_interactions(
        iter_feature_groups=iter_feature_groups,
        model_type=model_type,
        n_classes=n_classes,
        features_categorical = features_categorical, 
        features_bin_count = features_bin_count, 
        X=X_train,
        y=y_train,
        w=w_train,
        scores=scores_train,
        min_samples_leaf=min_samples_leaf,
    )
    return final_indices

def is_private(estimator):
    """Return True if the given estimator is a differentially private EBM estimator
    Parameters
    ----------
    estimator : estimator instance
        Estimator object to test.
    Returns
    -------
    out : bool
        True if estimator is a differentially private EBM estimator and False otherwise.
    """

    return isinstance(estimator, (DPExplainableBoostingClassifier, DPExplainableBoostingRegressor))

class BaseEBM(BaseEstimator):
    """Client facing SK EBM."""

    # Interface modeled after:
    # https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html
    # https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html
    # https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LinearRegression.html
    # https://scikit-learn.org/stable/modules/generated/sklearn.tree.DecisionTreeClassifier.html
    # https://scikit-learn.org/stable/modules/generated/sklearn.tree.DecisionTreeRegressor.html
    # https://xgboost.readthedocs.io/en/latest/python/python_api.html#module-xgboost.sklearn
    # https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.LGBMClassifier.html

    # TODO: order these parameters the same as our public parameter list
    def __init__(
        self,
        # Explainer
        #
        # feature_names in scikit-learn convention should probably be passed in via the fit function.  Also,
        #   we can get feature_names via pandas dataframes, and those would only be known at fit time, so
        #   we need a version of feature_names_out_ with the underscore to indicate items set at fit time.
        #   Despite this, we need to recieve a list of feature_names here to be compatible with blackbox explainations
        #   where we still need to have feature_names, but we do not have a fit function since we explain existing
        #   models without fitting them ourselves.  To conform to a common explaination API we get the feature_names
        #   here.
        feature_names,
        # other packages LightGBM, CatBoost, Scikit-Learn (future) are using categorical specific ways to indicate
        #   feature_types.  The benefit to them is that they can accept multiple ways of specifying categoricals like:
        #   categorical = [true, false, true, true] OR categorical = [1, 4, 8] OR categorical = 'all'/'auto'/'none'
        #   We're choosing a different route because for visualization we want to be able to express multiple
        #   different types of data.  For example, if the user has data with strings of "low", "medium", "high"
        #   We want to keep both the ordinal nature of this feature and we wish to preserve the text for visualization
        #   scikit-learn callers can pre-convert these things to [0, 1, 2] in the correct order because they don't
        #   need to worry about visualizing the data afterwards, but for us we  need a way to specify the strings
        #   back anyways.  So we need some way to express both the categorical nature of features and the order
        #   mapping.  We can do this and more complicated conversions via:
        #   feature_types = ["categorical", ["low", "medium", "high"], "continuous", "time", "bool"]
        feature_types,
        # Data
        #
        # Ensemble
        outer_bags,
        inner_bags,
        # Core
        # TODO PK v.3 replace mains in favor of a "boosting stage plan"
        mains,
        interactions,
        validation_size,
        max_rounds,
        early_stopping_tolerance,
        early_stopping_rounds,
        # Native
        learning_rate,
        # Holte, R. C. (1993) "Very simple classification rules perform well on most commonly used datasets"
        # says use 6 as the minimum samples https://link.springer.com/content/pdf/10.1023/A:1022631118932.pdf
        # TODO PK try setting this (not here, but in our caller) to 6 and run tests to verify the best value.
        min_samples_leaf,
        max_leaves,
        # Overall
        n_jobs,
        random_state,
        # Preprocessor
        binning,
        max_bins,
        max_interaction_bins,
        # Differential Privacy
        epsilon=None,
        delta=None,
        composition=None,
        bin_budget_frac=None,
        privacy_schema=None,
    ):
        # NOTE: Per scikit-learn convention, we shouldn't attempt to sanity check these inputs here.  We just
        #       Store these values for future use.  Validate inputs in the fit or other functions.  More details in:
        #       https://scikit-learn.org/stable/developers/develop.html

        # Arguments for explainer
        self.feature_names = feature_names
        self.feature_types = feature_types

        # Arguments for ensemble
        self.outer_bags = outer_bags
        if not is_private(self):
            self.inner_bags = inner_bags

        # Arguments for EBM beyond training a feature-step.
        self.mains = mains
        if not is_private(self):
            self.interactions = interactions
        self.validation_size = validation_size
        self.max_rounds = max_rounds
        if not is_private(self):
            self.early_stopping_tolerance = early_stopping_tolerance
            self.early_stopping_rounds = early_stopping_rounds

        # Arguments for internal EBM.
        self.learning_rate = learning_rate
        self.min_samples_leaf = min_samples_leaf
        self.max_leaves = max_leaves

        # Arguments for overall
        self.n_jobs = n_jobs
        self.random_state = random_state

        # Arguments for preprocessor
        self.binning = binning
        self.max_bins = max_bins
        if not is_private(self):
            self.max_interaction_bins = max_interaction_bins

        # Arguments for differential privacy
        if is_private(self):
            self.epsilon = epsilon
            self.delta = delta
            self.composition = composition
            self.bin_budget_frac = bin_budget_frac
            self.privacy_schema = privacy_schema

    def fit(self, X, y, sample_weight=None):  # noqa: C901
        """ Fits model to provided samples.

        Args:
            X: Numpy array for training samples.
            y: Numpy array as training labels.
            sample_weight: Optional array of weights per sample. Should be same length as X and y.

        Returns:
            Itself.
        """



        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        if is_classifier(self):
            y = clean_vector(y, True, "y")
            # use pure alphabetical ordering for the classes.  It's tempting to sort by frequency first
            # but that could lead to a lot of bugs if the # of categories is close and we flip the ordering
            # in two separate runs, which would flip the ordering of the classes within our score tensors.
            classes, y = np.unique(y, return_inverse=True)
        else:
            y = clean_vector(y, False, "y")
            classes = None

        if n_samples != len(y):
            msg = f"X has {n_samples} samples and y has {len(y)} samples"
            _log.error(msg)
            raise ValueError(msg)

        if sample_weight is not None:
            sample_weight = clean_vector(sample_weight, False, "sample_weight")
            if n_samples != len(sample_weight):
                msg = f"X has {n_samples} samples and sample_weight has {len(sample_weight)} samples"
                _log.error(msg)
                raise ValueError(msg)
        else:
            # TODO: eliminate this eventually
            sample_weight = np.ones_like(y, dtype=np.float64)





        # Privacy calculations
        noise_scale = None
        bin_eps_ = None
        bin_delta_ = None
        if is_private(self):
            DPUtils.validate_eps_delta(self.epsilon, self.delta)

            bounds = None if self.privacy_schema is None else self.privacy_schema.get('target', None)
            if bounds is None:
                # TODO: check with Harsha how domain_size should be handled for classification

                warn("Possible privacy violation: assuming min/max values for target are public info."
                     "Pass a privacy schema with known public target ranges to avoid this warning.")

                domain_size = y.max() - y.min()
            else:
                min_target = bounds[0]
                max_target = bounds[1]
                if max_target < min_target:
                    raise ValueError(f"target minimum {min_target} must be smaller than maximum {max_target}")
                domain_size = max_target - min_target

            # Split epsilon, delta budget for binning and learning
            bin_eps_ = self.epsilon * self.bin_budget_frac
            training_eps_ = self.epsilon - bin_eps_
            bin_delta_ = self.delta / 2
            training_delta_ = self.delta / 2


        if is_private(self):
            # TODO: remove the + 1 for max_bins and max_interaction_bins.  It's just here to compare to the previous results!
            bin_levels = [self.max_bins + 1]
        else:
            # TODO: remove the + 1 for max_bins and max_interaction_bins.  It's just here to compare to the previous results!
            bin_levels = [self.max_bins + 1, self.max_interaction_bins + 1]

        binning_result = construct_bins(
            X=X,
            feature_names_given=self.feature_names, 
            feature_types_given=self.feature_types, 
            max_bins_leveled=bin_levels, 
            binning=self.binning, 
            min_samples_bin=1, 
            min_unique_continuous=3, 
            epsilon=bin_eps_, 
            delta=bin_delta_, 
            privacy_schema=getattr(self, 'privacy_schema', None)
        )
        feature_names_in = binning_result[0]
        feature_types_in = binning_result[1]
        bins = binning_result[2]
        term_bin_counts = binning_result[3]
        min_vals = binning_result[4]
        max_vals = binning_result[5]
        histogram_cuts = binning_result[6]
        histogram_counts = binning_result[7]
        unique_counts = binning_result[8]
        zero_counts = binning_result[9]

        n_features_in = len(feature_names_in)

        if is_private(self):
             # [DP] Calculate how much noise will be applied to each iteration of the algorithm
            if self.composition == 'classic':
                noise_scale = DPUtils.calc_classic_noise_multi(
                    total_queries = self.max_rounds * n_features_in * self.outer_bags, 
                    target_epsilon = training_eps_, 
                    delta = training_delta_, 
                    sensitivity = domain_size * self.learning_rate * np.max(sample_weight)
                )
            elif self.composition == 'gdp':
                noise_scale = DPUtils.calc_gdp_noise_multi(
                    total_queries = self.max_rounds * n_features_in * self.outer_bags, 
                    target_epsilon = training_eps_, 
                    delta = training_delta_
                )
                noise_scale = noise_scale * domain_size * self.learning_rate * np.max(sample_weight) # Alg Line 17
            else:
                raise NotImplementedError(f"Unknown composition method provided: {self.composition}. Please use 'gdp' or 'classic'.")

        nominal_features = np.fromiter((x == 'nominal' for x in feature_types_in), dtype=ct.c_int64, count=len(feature_types_in))

        X_main, main_bin_counts = bin_python(X, 1, bins, feature_names_in,  feature_types_in)

        bin_data_counts = make_boosting_counts(term_bin_counts)

        native = Native.get_native_singleton()

        # scikit-learn returns an np.array for classification and
        # a single float for regression, so we do the same
        if is_classifier(self):
            model_type = "classification"

            n_classes = len(classes)
            if n_classes > 2:  # pragma: no cover
                warn("Multiclass is still experimental. Subject to change per release.")

            class_idx = {x: index for index, x in enumerate(classes)}
            intercept = np.zeros(
                Native.get_count_scores_c(n_classes), dtype=np.float64, order="C",
            )
        else:
            model_type = "regression"
            n_classes = -1
            intercept = 0.0

        provider = JobLibProvider(n_jobs=self.n_jobs)

        if isinstance(self.mains, str) and self.mains == "all":
            feature_groups = [[x] for x in range(n_features_in)]
        elif isinstance(self.mains, list) and all(
            isinstance(x, int) for x in self.mains
        ):
            feature_groups = [[x] for x in self.mains]
        else:  # pragma: no cover
            raise RuntimeError("Argument 'mains' has invalid value")
              
        # Train main effects
        if is_private(self):
            update = Native.GenerateUpdateOptions_GradientSums | Native.GenerateUpdateOptions_RandomSplits
        else:
            update = Native.GenerateUpdateOptions_Default

        init_seed = EBMUtils.normalize_initial_random_seed(self.random_state)

        inner_bags = 0 if is_private(self) else self.inner_bags
        early_stopping_rounds = -1 if is_private(self) else self.early_stopping_rounds
        early_stopping_tolerance = -1 if is_private(self) else self.early_stopping_tolerance

        train_model_args_iter = []
        bagged_seed = init_seed
        for idx in range(self.outer_bags):
            bagged_seed=native.generate_random_number(bagged_seed, 1416147523)
            parallel_params = (
                None,
                None,
                X_main,
                y,
                sample_weight,
                feature_groups,
                n_classes,
                self.validation_size,
                model_type,
                update,
                nominal_features,
                main_bin_counts,
                inner_bags,
                self.learning_rate,
                self.min_samples_leaf,
                self.max_leaves,
                early_stopping_rounds,
                early_stopping_tolerance,
                self.max_rounds,
                bagged_seed,
                noise_scale,
                bin_data_counts,
            )
            train_model_args_iter.append(parallel_params)

        results = provider.parallel(_parallel_cyclic_gradient_boost, train_model_args_iter)

        breakpoint_iteration = []
        only_models = []
        for model, bag_breakpoint_iteration in results:
            only_models.append(after_boosting(feature_groups, model, term_bin_counts))
            breakpoint_iteration.append(bag_breakpoint_iteration)

        bagged_additive_terms = []
        for term_idx in range(len(feature_groups)):
            bags = []
            bagged_additive_terms.append(bags)
            for model in only_models:
                bags.append(model[term_idx])


        interactions = 0 if is_private(self) else self.interactions
        if n_classes > 2 or isinstance(interactions, int) and interactions == 0 or isinstance(interactions, list) and len(interactions) == 0:
            del X_main # allow the garbage collector to dispose of X_main
            if not (isinstance(interactions, int) and interactions == 0 or isinstance(interactions, list) and len(interactions) == 0):
                warn("Detected multiclass problem: forcing interactions to 0")
        else:
            bagged_seed = init_seed
            scores_train_bags = []
            scores_val_bags = []
            for model in only_models:
                bagged_seed=native.generate_random_number(bagged_seed, 1416147523)

                scores_local = ebm_decision_function(X, n_samples, feature_names_in, feature_types_in, bins, intercept, model, feature_groups)

                _, _, _, _, _, _, scores_train_local, scores_val_local = EBMUtils.ebm_train_test_split(
                    X_main,
                    y,
                    sample_weight, # TODO: allow w to be None
                    test_size=self.validation_size,
                    random_state=bagged_seed,
                    is_classification=model_type == "classification",
                    scores=scores_local
                )
                scores_train_bags.append(scores_train_local)
                scores_val_bags.append(scores_val_local)
                scores_local = None # allow the garbage collector to reclaim this

            del X_main # allow the garbage collector to dispose of X_main

            X_pair, pair_bin_counts = bin_python(X, 2, bins, feature_names_in,  feature_types_in)

            if isinstance(interactions, int) and interactions > 0:
                _log.info("Estimating with FAST")

                train_model_args_iter2 = []
                bagged_seed = init_seed
                for i in range(self.outer_bags):
                    bagged_seed=native.generate_random_number(bagged_seed, 1416147523)
                    parallel_params = (
                        scores_train_bags[i],
                        X_pair, 
                        y, 
                        sample_weight, 
                        n_classes,
                        self.validation_size, 
                        bagged_seed, 
                        model_type, 
                        nominal_features, 
                        pair_bin_counts, 
                        self.min_samples_leaf, 
                    )
                    train_model_args_iter2.append(parallel_params)

                bagged_interaction_indices = provider.parallel(_parallel_get_interactions, train_model_args_iter2)

                # Select merged pairs
                pair_ranks = {}
                for n, interaction_indices in enumerate(bagged_interaction_indices):
                    for rank, indices in enumerate(interaction_indices):
                        old_mean = pair_ranks.get(indices, 0)
                        pair_ranks[indices] = old_mean + ((rank - old_mean) / (n + 1))

                final_ranks = []
                total_interactions = 0
                for indices in pair_ranks:
                    heapq.heappush(final_ranks, (pair_ranks[indices], indices))
                    total_interactions += 1

                n_interactions = min(interactions, total_interactions)
                pair_indices = [heapq.heappop(final_ranks)[1] for _ in range(n_interactions)]

            elif isinstance(interactions, list):
                pair_indices = interactions
                # Check and remove duplicate interaction terms
                existing_terms = set()
                unique_terms = []

                for i, term in enumerate(pair_indices):
                    sorted_tuple = tuple(sorted(term))
                    if sorted_tuple not in existing_terms:
                        existing_terms.add(sorted_tuple)
                        unique_terms.append(term)

                # Warn the users that we have made change to the interactions list
                if len(unique_terms) != len(pair_indices):
                    warn("Detected duplicate interaction terms: removing duplicate interaction terms")
                    pair_indices = unique_terms

            else:  # pragma: no cover
                raise RuntimeError("Argument 'interaction' has invalid value")

            feature_groups.extend(pair_indices)

            staged_fit_args_iter = []
            bagged_seed = init_seed
            for i in range(self.outer_bags):
                bagged_seed=native.generate_random_number(bagged_seed, 1416147523)
                parallel_params = (
                    scores_train_bags[i],
                    scores_val_bags[i],
                    X_pair, 
                    y, 
                    sample_weight, 
                    pair_indices, 
                    n_classes, 
                    self.validation_size, 
                    model_type, 
                    update,
                    nominal_features, 
                    pair_bin_counts, 
                    inner_bags, 
                    self.learning_rate, 
                    self.min_samples_leaf, 
                    self.max_leaves, 
                    early_stopping_rounds, 
                    early_stopping_tolerance, 
                    self.max_rounds, 
                    bagged_seed, 
                    noise_scale, 
                    bin_data_counts, 
                )
                staged_fit_args_iter.append(parallel_params)

            del X_pair # allow the garbage collector to dispose of X_pair

            results = provider.parallel(_parallel_cyclic_gradient_boost, staged_fit_args_iter)

            only_models = []
            for model, bag_breakpoint_iteration in results:
                breakpoint_iteration.append(bag_breakpoint_iteration)
                only_models.append(after_boosting(pair_indices, model, term_bin_counts))

            for term_idx in range(len(pair_indices)):
                bags = []
                bagged_additive_terms.append(bags)
                for model in only_models:
                    bags.append(model[term_idx])

        if is_private(self):
            # TODO: currently we're getting counts out of the binning code.  We need to instead return
            #       term_bin_weights and then this code will be correct.
            bin_counts = None
            bin_weights = [None] * len(feature_groups)
            for feature_group_idx, feature_group in enumerate(feature_groups):
                feature_idx = feature_group[0] # for now we only support mains for DP models
                bin_weights[feature_group_idx] = term_bin_counts[feature_idx]
        else:
            bin_counts, bin_weights = get_counts_and_weights(X, sample_weight, feature_names_in, feature_types_in, bins, feature_groups)

        additive_terms = []
        term_standard_deviations = []
        for score_tensors in bagged_additive_terms:
            # TODO PK: shouldn't we be zero centering each score tensor first before taking the standard deviation
            # It's possible to shift scores arbitary to the intercept, so we should be able to get any desired stddev

            all_score_tensors = np.array(score_tensors)
            averaged_model = np.average(all_score_tensors, axis=0)
            model_errors = np.std(all_score_tensors, axis=0)
            additive_terms.append(averaged_model)
            term_standard_deviations.append(model_errors)

        if n_classes <= 2:
            for set_idx in range(len(feature_groups)):
                score_mean = np.average(additive_terms[set_idx], weights=bin_weights[set_idx])
                additive_terms[set_idx] = (additive_terms[set_idx] - score_mean)

                # Add mean center adjustment back to intercept
                intercept += score_mean
        else:
            # Postprocess model graphs for multiclass
            multiclass_postprocess2(n_classes, n_samples, additive_terms, intercept, bin_weights)

        restore_missing_value_zeros2(additive_terms, bin_weights)
        restore_missing_value_zeros2(term_standard_deviations, bin_weights)

        feature_importances = []
        for i in range(len(feature_groups)):
            # TODO: change this to use bin_weights ALWAYS after we're done comparing/testing this
            avg_bins_weights = bin_weights if bin_counts is None else bin_counts

            mean_abs_score = np.abs(additive_terms[i])
            if 2 < n_classes:
                mean_abs_score = np.average(mean_abs_score, axis=mean_abs_score.ndim - 1)
            mean_abs_score = np.average(mean_abs_score, weights=avg_bins_weights[i])
            feature_importances.append(mean_abs_score)

        # using numpy operations can change this to np.float64, but scikit-learn uses a float for regression
        if not is_classifier(self):
            intercept = float(intercept)

        if is_private(self):
            # TODO: check with Harsha that these need to be preserved, or if other properties should be as well
            # TODO: consider recording 'min_target' and 'max_target' in all models, not just DP and remove the domain_size
            self.domain_size_ = domain_size
            # TODO: make noise_scale a property?  We can re-calculate it after fitting since we need to know n_features_in_
            # we could make an internal function to calcualte it and pass it n_features_in_ after we've been fit
            # but also use it here to calculate the noise_scale
            self.noise_scale_ = noise_scale
        if 0 <= n_classes:
            self.classes_ = classes # required by scikit-learn
            self._class_idx_ = class_idx
        self.n_samples_ = n_samples
        self.n_features_in_ = n_features_in # required by scikit-learn
        self.feature_names_in_ = feature_names_in
        self.feature_types_in_ = feature_types_in
        self.bins_ = bins
        self.bin_counts_ = bin_counts
        self.bin_weights_ = bin_weights
        self.min_vals_ = min_vals
        self.max_vals_ = max_vals
        self.histogram_cuts_ = histogram_cuts
        self.histogram_counts_ = histogram_counts
        self.unique_counts_ = unique_counts
        self.zero_counts_ = zero_counts
        self.bagged_additive_terms_ = bagged_additive_terms
        self.additive_terms_ = additive_terms
        self.intercept_ = intercept
        self.term_standard_deviations_ = term_standard_deviations
        self.feature_importances_ = feature_importances
        self.feature_groups_ = feature_groups
        self.breakpoint_iteration_ = breakpoint_iteration
        self.has_fitted_ = True
        return self

    def decision_function(self, X):
        """ Predict scores from model before calling the link function.

            Args:
                X: Numpy array for samples.

            Returns:
                The sum of the additive term contributions.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        return ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

    def explain_global(self, name=None):
        """ Provides global explanation for model.

        Args:
            name: User-defined explanation name.

        Returns:
            An explanation object,
            visualizing feature-value pairs as horizontal bar chart.
        """
        if name is None:
            name = gen_name_from_class(self)

        check_is_fitted(self, "has_fitted_")

        mod_counts = remove_last2(self.bin_weights_ if self.bin_counts_ is None else self.bin_counts_, self.bin_weights_)
        mod_additive_terms = remove_last2(self.additive_terms_, self.bin_weights_)
        mod_term_standard_deviations = remove_last2(self.term_standard_deviations_, self.bin_weights_)
        for feature_group_idx, feature_group in enumerate(self.feature_groups_):
            mod_additive_terms[feature_group_idx] = trim_tensor(mod_additive_terms[feature_group_idx], trim_low=[True] * len(feature_group))
            mod_term_standard_deviations[feature_group_idx] = trim_tensor(mod_term_standard_deviations[feature_group_idx], trim_low=[True] * len(feature_group))
            mod_counts[feature_group_idx] = trim_tensor(mod_counts[feature_group_idx], trim_low=[True] * len(feature_group))

        # Obtain min/max for model scores
        lower_bound = np.inf
        upper_bound = -np.inf
        for feature_group_index, _ in enumerate(self.feature_groups_):
            errors = mod_term_standard_deviations[feature_group_index]
            scores = mod_additive_terms[feature_group_index]

            lower_bound = min(lower_bound, np.min(scores - errors))
            upper_bound = max(upper_bound, np.max(scores + errors))

        bounds = (lower_bound, upper_bound)

        # Add per feature graph
        data_dicts = []
        feature_list = []
        density_list = []
        for feature_group_index, feature_indexes in enumerate(
            self.feature_groups_
        ):
            model_graph = mod_additive_terms[feature_group_index]

            # NOTE: This uses stddev. for bounds, consider issue warnings.
            errors = mod_term_standard_deviations[feature_group_index]

            if len(feature_indexes) == 1:
                feature_index0 = feature_indexes[0]

                feature_bins = self.bins_[feature_index0][0]
                if isinstance(feature_bins, dict):
                    # categorical
                    bin_labels = list(feature_bins.keys())
                    if len(bin_labels) != model_graph.shape[0]:
                        bin_labels.append('DPOther')

                    names=bin_labels
                    densities = list(mod_counts[feature_group_index])
                else:
                    # continuous
                    min_val = self.min_vals_[feature_index0]
                    max_val = self.max_vals_[feature_index0]
                    bin_labels = list(np.concatenate(([min_val], feature_bins, [max_val])))

                    if is_private(self):
                        names = feature_bins
                        densities = list(mod_counts[feature_group_index])
                    else:
                        names = self.histogram_cuts_[feature_index0]
                        densities = list(self.histogram_counts_[feature_index0][1:-1])
                    names = list(np.concatenate(([min_val], names, [max_val])))

                scores = list(model_graph)
                upper_bounds = list(model_graph + errors)
                lower_bounds = list(model_graph - errors)
                density_dict = {
                    "names": names,
                    "scores": densities,
                }

                feature_dict = {
                    "type": "univariate",
                    "names": bin_labels,
                    "scores": scores,
                    "scores_range": bounds,
                    "upper_bounds": upper_bounds,
                    "lower_bounds": lower_bounds,
                }
                feature_list.append(feature_dict)
                density_list.append(density_dict)

                data_dict = {
                    "type": "univariate",
                    "names": bin_labels,
                    "scores": model_graph,
                    "scores_range": bounds,
                    "upper_bounds": model_graph + errors,
                    "lower_bounds": model_graph - errors,
                    "density": {
                        "names": names,
                        "scores": densities,
                    },
                }
                if is_classifier(self):
                    data_dict["meta"] = {
                        "label_names": self.classes_.tolist()  # Classes should be numpy array, convert to list.
                    }

                data_dicts.append(data_dict)
            elif len(feature_indexes) == 2:
                bin_levels = self.bins_[feature_indexes[0]]
                feature_bins = bin_levels[1] if 1 < len(bin_levels) else bin_levels[0]
                if isinstance(feature_bins, dict):
                    # categorical
                    bin_labels = list(feature_bins.keys())
                    if len(bin_labels) != model_graph.shape[0]:
                        bin_labels.append('DPOther')
                else:
                    # continuous
                    min_val = self.min_vals_[feature_indexes[0]]
                    max_val = self.max_vals_[feature_indexes[0]]
                    bin_labels = list(np.concatenate(([min_val], feature_bins, [max_val])))
                bin_labels_left = bin_labels


                bin_levels = self.bins_[feature_indexes[1]]
                feature_bins = bin_levels[1] if 1 < len(bin_levels) else bin_levels[0]
                if isinstance(feature_bins, dict):
                    # categorical
                    bin_labels = list(feature_bins.keys())
                    if len(bin_labels) != model_graph.shape[1]:
                        bin_labels.append('DPOther')
                else:
                    # continuous
                    min_val = self.min_vals_[feature_indexes[1]]
                    max_val = self.max_vals_[feature_indexes[1]]
                    bin_labels = list(np.concatenate(([min_val], feature_bins, [max_val])))
                bin_labels_right = bin_labels


                feature_dict = {
                    "type": "interaction",
                    "left_names": bin_labels_left,
                    "right_names": bin_labels_right,
                    "scores": model_graph,
                    "scores_range": bounds,
                }
                feature_list.append(feature_dict)
                density_list.append({})

                data_dict = {
                    "type": "interaction",
                    "left_names": bin_labels_left,
                    "right_names": bin_labels_right,
                    "scores": model_graph,
                    "scores_range": bounds,
                }
                data_dicts.append(data_dict)
            else:  # pragma: no cover
                raise Exception("Interactions greater than 2 not supported.")

        overall_dict = {
            "type": "univariate",
            "names": self.term_names_,
            "scores": self.feature_importances_,
        }
        internal_obj = {
            "overall": overall_dict,
            "specific": data_dicts,
            "mli": [
                {
                    "explanation_type": "ebm_global",
                    "value": {"feature_list": feature_list},
                },
                {"explanation_type": "density", "value": {"density": density_list}},
            ],
        }

        return EBMExplanation(
            "global",
            internal_obj,
            feature_names=self.term_names_,
            feature_types=['categorical' if x == 'nominal' or x == 'ordinal' else x for x in self.term_types_],
            name=name,
            selector=gen_global_selector2(self.n_samples_, self.n_features_in_, self.term_names_, ['categorical' if x == 'nominal' or x == 'ordinal' else x for x in self.term_types_], self.unique_counts_, self.zero_counts_),
        )

    def explain_local(self, X, y=None, name=None):
        """ Provides local explanations for provided samples.

        Args:
            X: Numpy array for X to explain.
            y: Numpy vector for y to explain.
            name: User-defined explanation name.

        Returns:
            An explanation object, visualizing feature-value pairs
            for each sample as horizontal bar charts.
        """

        # Produce feature value pairs for each sample.
        # Values are the model graph score per respective feature group.
        if name is None:
            name = gen_name_from_class(self)

        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        X_unified, y, _, classes_temp, _, _ = unify_data2(
            is_classifier(self), 
            X, 
            y, 
            None, 
            feature_names=self.feature_names_in_, 
            feature_types=self.feature_types_in_
        )

        if is_classifier(self) and y is not None:
            y = np.array([self._class_idx_[el] for el in classes_temp[y]], dtype=np.int64)

        data_dicts = []
        intercept = self.intercept_
        if not is_classifier(self) or len(self.classes_) <= 2:
            if isinstance(self.intercept_, np.ndarray) or isinstance(
                self.intercept_, list
            ):
                intercept = intercept[0]

        for _ in range(n_samples):
            data_dict = {
                "type": "univariate",
                "names": [None] * len(self.feature_groups_),
                "scores": [None] * len(self.feature_groups_),
                "values": [None] * len(self.feature_groups_),
                "extra": {"names": ["Intercept"], "scores": [intercept], "values": [1]},
            }
            if is_classifier(self):
                data_dict["meta"] = {
                    "label_names": self.classes_.tolist()  # Classes should be numpy array, convert to list.
                }
            data_dicts.append(data_dict)

        term_names = self.term_names_
        for set_idx, binned_data in eval_terms(X, self.feature_names_in_, self.feature_types_in_, self.bins_, self.feature_groups_):
            scores = self.additive_terms_[set_idx][tuple(binned_data)]
            feature_group = self.feature_groups_[set_idx]
            for row_idx in range(n_samples):
                feature_name = term_names[set_idx]
                data_dicts[row_idx]["names"][set_idx] = feature_name
                data_dicts[row_idx]["scores"][set_idx] = scores[row_idx]
                if len(feature_group) == 1:
                    data_dicts[row_idx]["values"][set_idx] = X_unified[row_idx, feature_group[0]]
                else:
                    data_dicts[row_idx]["values"][set_idx] = ""

        is_classification = is_classifier(self)

        scores = ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        if is_classification:
            # Handle binary classification case -- softmax only works with 0s appended
            if scores.ndim == 1:
                scores = np.c_[np.zeros(scores.shape), scores]

            scores = softmax(scores)

        perf_list = []
        perf_dicts = gen_perf_dicts(scores, y, is_classification)
        for row_idx in range(n_samples):
            perf = None if perf_dicts is None else perf_dicts[row_idx]
            perf_list.append(perf)
            data_dicts[row_idx]["perf"] = perf

        selector = gen_local_selector(data_dicts, is_classification=is_classification)

        additive_terms = remove_last2(self.additive_terms_, self.bin_weights_)
        for feature_group_idx, feature_group in enumerate(self.feature_groups_):
            additive_terms[feature_group_idx] = trim_tensor(additive_terms[feature_group_idx], trim_low=[True] * len(feature_group))

        internal_obj = {
            "overall": None,
            "specific": data_dicts,
            "mli": [
                {
                    "explanation_type": "ebm_local",
                    "value": {
                        "scores": additive_terms,
                        "intercept": self.intercept_,
                        "perf": perf_list,
                    },
                }
            ],
        }
        internal_obj["mli"].append(
            {
                "explanation_type": "evaluation_dataset",
                "value": {"dataset_x": X_unified, "dataset_y": y},
            }
        )

        return EBMExplanation(
            "local",
            internal_obj,
            feature_names=self.term_names_,
            feature_types=['categorical' if x == 'nominal' or x == 'ordinal' else x for x in self.term_types_],
            name=name,
            selector=selector,
        )


    @property
    def term_names_(self):
        return [EBMUtils.gen_feature_group_name(feature_idxs, self.feature_names_in_) for feature_idxs in self.feature_groups_]

    @property
    def term_types_(self):
        return [EBMUtils.gen_feature_group_type(feature_idxs, self.feature_types_in_) for feature_idxs in self.feature_groups_]


class ExplainableBoostingClassifier(BaseEBM, ClassifierMixin, ExplainerMixin):
    """ Explainable Boosting Classifier. The arguments will change in a future release, watch the changelog. """

    # TODO PK v.3 use underscores here like ClassifierMixin._estimator_type?
    available_explanations = ["global", "local"]
    explainer_type = "model"

    """ Public facing EBM classifier."""

    def __init__(
        self,
        # Explainer
        feature_names=None,
        feature_types=None,
        # Preprocessor
        max_bins=256,
        max_interaction_bins=32,
        binning="quantile",
        # Stages
        mains="all",
        interactions=10,
        # Ensemble
        outer_bags=8,
        inner_bags=0,
        # Boosting
        learning_rate=0.01,
        validation_size=0.15,
        early_stopping_rounds=50,
        early_stopping_tolerance=1e-4,
        max_rounds=5000,
        # Trees
        min_samples_leaf=2,
        max_leaves=3,
        # Overall
        n_jobs=-2,
        random_state=42,
    ):
        """ Explainable Boosting Classifier. The arguments will change in a future release, watch the changelog.

        Args:
            feature_names: List of feature names.
            feature_types: List of feature types.
            max_bins: Max number of bins per feature for pre-processing stage.
            max_interaction_bins: Max number of bins per feature for pre-processing stage on interaction terms. Only used if interactions is non-zero.
            binning: Method to bin values for pre-processing. Choose "uniform", "quantile" or "quantile_humanized".
            mains: Features to be trained on in main effects stage. Either "all" or a list of feature indexes.
            interactions: Interactions to be trained on.
                Either a list of lists of feature indices, or an integer for number of automatically detected interactions.
                Interactions are forcefully set to 0 for multiclass problems.
            outer_bags: Number of outer bags.
            inner_bags: Number of inner bags.
            learning_rate: Learning rate for boosting.
            validation_size: Validation set size for boosting.
            early_stopping_rounds: Number of rounds of no improvement to trigger early stopping.
            early_stopping_tolerance: Tolerance that dictates the smallest delta required to be considered an improvement.
            max_rounds: Number of rounds for boosting.
            min_samples_leaf: Minimum number of cases for tree splits used in boosting.
            max_leaves: Maximum leaf nodes used in boosting.
            n_jobs: Number of jobs to run in parallel.
            random_state: Random state.
        """
        super(ExplainableBoostingClassifier, self).__init__(
            # Explainer
            feature_names=feature_names,
            feature_types=feature_types,
            # Preprocessor
            max_bins=max_bins,
            max_interaction_bins=max_interaction_bins,
            binning=binning,
            # Stages
            mains=mains,
            interactions=interactions,
            # Ensemble
            outer_bags=outer_bags,
            inner_bags=inner_bags,
            # Boosting
            learning_rate=learning_rate,
            validation_size=validation_size,
            early_stopping_rounds=early_stopping_rounds,
            early_stopping_tolerance=early_stopping_tolerance,
            max_rounds=max_rounds,
            # Trees
            min_samples_leaf=min_samples_leaf,
            max_leaves=max_leaves,
            # Overall
            n_jobs=n_jobs,
            random_state=random_state,
        )

    def predict_proba(self, X):
        """ Probability estimates on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Probability estimate of sample for each class.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        log_odds_vector = ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        # Handle binary classification case -- softmax only works with 0s appended
        if log_odds_vector.ndim == 1:
            log_odds_vector = np.c_[np.zeros(log_odds_vector.shape), log_odds_vector]

        return softmax(log_odds_vector)

    def predict(self, X):
        """ Predicts on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Predicted class label per sample.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        log_odds_vector = ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        # Handle binary classification case -- softmax only works with 0s appended
        if log_odds_vector.ndim == 1:
            log_odds_vector = np.c_[np.zeros(log_odds_vector.shape), log_odds_vector]

        return self.classes_[np.argmax(log_odds_vector, axis=1)]

    def predict_and_contrib(self, X, output='probabilities'):
        """Predicts on provided samples, returning predictions and explanations for each sample.

        Args:
            X: Numpy array for samples.
            output: Prediction type to output (i.e. one of 'probabilities', 'logits', 'labels')

        Returns:
            Predictions and local explanations for each sample.
        """

        allowed_outputs = ['probabilities', 'logits', 'labels']
        if output not in allowed_outputs:
            msg = "Argument 'output' has invalid value.  Got '{}', expected one of " 
            + repr(allowed_outputs)
            raise ValueError(msg.format(output))

        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        scores, explanations = ebm_decision_function_and_explain(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        if output == 'probabilities':
            if scores.ndim == 1:
                scores= np.c_[np.zeros(scores.shape), scores]
            result = softmax(scores)
        elif output == 'labels':
            if scores.ndim == 1:
                scores = np.c_[np.zeros(scores.shape), scores]
            result = self.classes_[np.argmax(scores, axis=1)]
        else:
            result = scores

        return result, explanations

class ExplainableBoostingRegressor(BaseEBM, RegressorMixin, ExplainerMixin):
    """ Explainable Boosting Regressor. The arguments will change in a future release, watch the changelog. """

    # TODO PK v.3 use underscores here like RegressorMixin._estimator_type?
    available_explanations = ["global", "local"]
    explainer_type = "model"

    """ Public facing EBM regressor."""

    def __init__(
        self,
        # Explainer
        feature_names=None,
        feature_types=None,
        # Preprocessor
        max_bins=256,
        max_interaction_bins=32,
        binning="quantile",
        # Stages
        mains="all",
        interactions=10,
        # Ensemble
        outer_bags=8,
        inner_bags=0,
        # Boosting
        learning_rate=0.01,
        validation_size=0.15,
        early_stopping_rounds=50,
        early_stopping_tolerance=1e-4,
        max_rounds=5000,
        # Trees
        min_samples_leaf=2,
        max_leaves=3,
        # Overall
        n_jobs=-2,
        random_state=42,
    ):
        """ Explainable Boosting Regressor. The arguments will change in a future release, watch the changelog.

        Args:
            feature_names: List of feature names.
            feature_types: List of feature types.
            max_bins: Max number of bins per feature for pre-processing stage on main effects.
            max_interaction_bins: Max number of bins per feature for pre-processing stage on interaction terms. Only used if interactions is non-zero.
            binning: Method to bin values for pre-processing. Choose "uniform", "quantile", or "quantile_humanized".
            mains: Features to be trained on in main effects stage. Either "all" or a list of feature indexes.
            interactions: Interactions to be trained on.
                Either a list of lists of feature indices, or an integer for number of automatically detected interactions.
            outer_bags: Number of outer bags.
            inner_bags: Number of inner bags.
            learning_rate: Learning rate for boosting.
            validation_size: Validation set size for boosting.
            early_stopping_rounds: Number of rounds of no improvement to trigger early stopping.
            early_stopping_tolerance: Tolerance that dictates the smallest delta required to be considered an improvement.
            max_rounds: Number of rounds for boosting.
            min_samples_leaf: Minimum number of cases for tree splits used in boosting.
            max_leaves: Maximum leaf nodes used in boosting.
            n_jobs: Number of jobs to run in parallel.
            random_state: Random state.
        """
        super(ExplainableBoostingRegressor, self).__init__(
            # Explainer
            feature_names=feature_names,
            feature_types=feature_types,
            # Preprocessor
            max_bins=max_bins,
            max_interaction_bins=max_interaction_bins,
            binning=binning,
            # Stages
            mains=mains,
            interactions=interactions,
            # Ensemble
            outer_bags=outer_bags,
            inner_bags=inner_bags,
            # Boosting
            learning_rate=learning_rate,
            validation_size=validation_size,
            early_stopping_rounds=early_stopping_rounds,
            early_stopping_tolerance=early_stopping_tolerance,
            max_rounds=max_rounds,
            # Trees
            min_samples_leaf=min_samples_leaf,
            max_leaves=max_leaves,
            # Overall
            n_jobs=n_jobs,
            random_state=random_state,
        )

    def predict(self, X):
        """ Predicts on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Predicted class label per sample.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        return ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

    def predict_and_contrib(self, X):
        """Predicts on provided samples, returning predictions and explanations for each sample.

        Args:
            X: Numpy array for samples.

        Returns:
            Predictions and local explanations for each sample.
        """

        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        return ebm_decision_function_and_explain(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )


class DPExplainableBoostingClassifier(BaseEBM, ClassifierMixin, ExplainerMixin):
    """ Differentially Private Explainable Boosting Classifier."""

    available_explanations = ["global", "local"]
    explainer_type = "model"

    """ Public facing DPEBM classifier."""

    def __init__(
        self,
        # Explainer
        feature_names=None,
        feature_types=None,
        # Preprocessor
        max_bins=32,
        binning="private",
        # Stages
        mains="all",
        # Ensemble
        outer_bags=1,
        # Boosting
        learning_rate=0.01,
        validation_size=0,
        max_rounds=300,
        # Trees
        min_samples_leaf=2,
        max_leaves=3,
        # Overall
        n_jobs=-2,
        random_state=42,
        # Differential Privacy
        epsilon=1,
        delta=1e-5,
        composition='gdp',
        bin_budget_frac=0.1,
        privacy_schema=None,
    ):
        """ Differentially Private Explainable Boosting Classifier. Note that many arguments are defaulted differently than regular EBMs.

        Args:
            feature_names: List of feature names.
            feature_types: List of feature types.
            max_bins: Max number of bins per feature for pre-processing stage.
            binning: Method to bin values for pre-processing. Choose "uniform" or "quantile".
            mains: Features to be trained on in main effects stage. Either "all" or a list of feature indexes.
            outer_bags: Number of outer bags.
            learning_rate: Learning rate for boosting.
            validation_size: Validation set size for boosting.
            max_rounds: Number of rounds for boosting.
            max_leaves: Maximum leaf nodes used in boosting.
            min_samples_leaf: Minimum number of cases for tree splits used in boosting.
            n_jobs: Number of jobs to run in parallel.
            random_state: Random state.
            epsilon: Total privacy budget to be spent across all rounds of training.
            delta: Additive component of differential privacy guarantee. Should be smaller than 1/n_training_samples.
            composition: composition.
            bin_budget_frac: Percentage of total epsilon budget to use for binning.
            privacy_schema: Dictionary specifying known min/max values of each feature and target. 
                If None, DP-EBM throws warning and uses data to calculate these values.
        """
        super(DPExplainableBoostingClassifier, self).__init__(
            # Explainer
            feature_names=feature_names,
            feature_types=feature_types,    
            # Preprocessor
            max_bins=max_bins,
            max_interaction_bins=None,
            binning=binning,
            # Stages
            mains=mains,
            interactions=0,
            # Ensemble
            outer_bags=outer_bags,
            inner_bags=0,
            # Boosting
            learning_rate=learning_rate,
            validation_size=validation_size,
            early_stopping_rounds=-1,
            early_stopping_tolerance=-1,
            max_rounds=max_rounds,
            # Trees
            min_samples_leaf=min_samples_leaf,
            max_leaves=max_leaves,
            # Overall
            n_jobs=n_jobs,
            random_state=random_state,
            # Differential Privacy
            epsilon=epsilon,
            delta=delta,
            composition=composition,
            bin_budget_frac=bin_budget_frac,
            privacy_schema=privacy_schema,
        )

    def predict_proba(self, X):
        """ Probability estimates on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Probability estimate of sample for each class.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        log_odds_vector = ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        # Handle binary classification case -- softmax only works with 0s appended
        if log_odds_vector.ndim == 1:
            log_odds_vector = np.c_[np.zeros(log_odds_vector.shape), log_odds_vector]

        return softmax(log_odds_vector)

    def predict(self, X):
        """ Predicts on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Predicted class label per sample.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        log_odds_vector = ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

        # Handle binary classification case -- softmax only works with 0s appended
        if log_odds_vector.ndim == 1:
            log_odds_vector = np.c_[np.zeros(log_odds_vector.shape), log_odds_vector]

        return self.classes_[np.argmax(log_odds_vector, axis=1)]


class DPExplainableBoostingRegressor(BaseEBM, RegressorMixin, ExplainerMixin):
    """ Differentially Private Explainable Boosting Regressor."""

    # TODO PK v.3 use underscores here like RegressorMixin._estimator_type?
    available_explanations = ["global", "local"]
    explainer_type = "model"

    """ Public facing DPEBM regressor."""

    def __init__(
        self,
        # Explainer
        feature_names=None,
        feature_types=None,
        # Preprocessor
        max_bins=32,
        binning="private",
        # Stages
        mains="all",
        # Ensemble
        outer_bags=1,
        # Boosting
        learning_rate=0.01,
        validation_size=0,
        max_rounds=300,
        # Trees
        min_samples_leaf=2,
        max_leaves=3,
        # Overall
        n_jobs=-2,
        random_state=42,
        # Differential Privacy
        epsilon=1,
        delta=1e-5,
        composition='gdp',
        bin_budget_frac=0.1,
        privacy_schema=None,
    ):
        """ Differentially Private Explainable Boosting Regressor. Note that many arguments are defaulted differently than regular EBMs.

        Args:
            feature_names: List of feature names.
            feature_types: List of feature types.
            max_bins: Max number of bins per feature for pre-processing stage.
            binning: Method to bin values for pre-processing. Choose "uniform" or "quantile".
            mains: Features to be trained on in main effects stage. Either "all" or a list of feature indexes.
            outer_bags: Number of outer bags.
            learning_rate: Learning rate for boosting.
            validation_size: Validation set size for boosting.
            max_rounds: Number of rounds for boosting.
            max_leaves: Maximum leaf nodes used in boosting.
            min_samples_leaf: Minimum number of cases for tree splits used in boosting.
            n_jobs: Number of jobs to run in parallel.
            random_state: Random state.
            epsilon: Total privacy budget to be spent across all rounds of training.
            delta: Additive component of differential privacy guarantee. Should be smaller than 1/n_training_samples.
            composition: Method of tracking noise aggregation. Must be one of 'classic' or 'gdp'. 
            bin_budget_frac: Percentage of total epsilon budget to use for private binning.
            privacy_schema: Dictionary specifying known min/max values of each feature and target. 
                If None, DP-EBM throws warning and uses data to calculate these values.
        """
        super(DPExplainableBoostingRegressor, self).__init__(
            # Explainer
            feature_names=feature_names,
            feature_types=feature_types,
            # Preprocessor
            max_bins=max_bins,
            max_interaction_bins=None,
            binning=binning,
            # Stages
            mains=mains,
            interactions=0,
            # Ensemble
            outer_bags=outer_bags,
            inner_bags=0,
            # Boosting
            learning_rate=learning_rate,
            validation_size=validation_size,
            early_stopping_rounds=-1,
            early_stopping_tolerance=-1,
            max_rounds=max_rounds,
            # Trees
            min_samples_leaf=min_samples_leaf,
            max_leaves=max_leaves,
            # Overall
            n_jobs=n_jobs,
            random_state=random_state,
            # Differential Privacy
            epsilon=epsilon,
            delta=delta,
            composition=composition,
            bin_budget_frac=bin_budget_frac,
            privacy_schema=privacy_schema,
        )

    def predict(self, X):
        """ Predicts on provided samples.

        Args:
            X: Numpy array for samples.

        Returns:
            Predicted class label per sample.
        """
        check_is_fitted(self, "has_fitted_")

        X, n_samples = clean_X(X)
        if n_samples <= 0:
            msg = "X has no samples to train on"
            _log.error(msg)
            raise ValueError(msg)

        return ebm_decision_function(
            X, 
            n_samples, 
            self.feature_names_in_, 
            self.feature_types_in_, 
            self.bins_, 
            self.intercept_, 
            self.additive_terms_, 
            self.feature_groups_
        )

