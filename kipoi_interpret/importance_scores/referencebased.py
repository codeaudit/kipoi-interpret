from __future__ import division, absolute_import, print_function
from .base import ImportanceScoreWRef
from .gradient import Gradient
from kipoi.model import KerasModel, TensorFlowModel
from kipoi.data_utils import numpy_collate

import tempfile
import numpy as np


# Other proposal (this object is passed as an argument to compile())
class DeepLift(ImportanceScoreWRef):

    def __init__(self, model, output_layer,
                 task_idx, preact, mxts_mode = 'rescale_conv_revealcancel_fc',
                 batch_size = 32):
        from deeplift.conversion import kerasapi_conversion as kc
        from deeplift.layers import NonlinearMxtsMode

        def get_mxts_mode(mode_name):
            # Labels from examples:
            mxts_modes = {'rescale_conv_revealcancel_fc': NonlinearMxtsMode.DeepLIFT_GenomicsDefault,
                          'revealcancel_all_layers': NonlinearMxtsMode.RevealCancel,
                          'rescale_all_layers': NonlinearMxtsMode.Rescale,
                          'grad_times_inp': NonlinearMxtsMode.Gradient,
                          'guided_backprop': NonlinearMxtsMode.GuidedBackprop}
            return mxts_modes[mode_name]

        # TODO: create and return the deeplift func, which
        # takes arguments "input_data_list" and "input_references_list"
        self.model = model
        if not self.is_compatible(model):
            raise Exception("Model not compatible with DeepLift")

        self.task_idx = task_idx
        self.batch_size = batch_size

        weight_f = tempfile.mktemp()
        arch_f = tempfile.mktemp()
        model.model.save_weights(weight_f)
        with open(arch_f, "w") as ofh:
            ofh.write(model.model.to_json())
        self.deeplift_model = kc.convert_model_from_saved_files(weight_f, json_file=arch_f,
                                                                nonlinear_mxts_mode=get_mxts_mode(mxts_mode))
        input_names = self.model._get_feed_input_names()
        self.input_layer_idxs = []
        self.output_layers_idxs = []
        for input_name in input_names:
            input_layer_name = input_name[:-len("_input")] if input_name.endswith("_input") else input_name
            for i, l in enumerate(self.model.model.layers):
                if l.name == input_layer_name:
                    self.input_layer_idxs.append(i)

        self.fwd_predict_fn = None

        # Compile the function that computes the contribution scores
        # For sigmoid or softmax outputs, target_layer_idx should be -2 (the default)
        # (See "3.6 Choice of target layer" in https://arxiv.org/abs/1704.02685 for justification)
        # For regression tasks with a linear output, target_layer_idx should be -1
        # (which simply refers to the last layer)
        # If you want the DeepLIFT multipliers instead of the contribution scores, you can use get_target_multipliers_func
        self.deeplift_contribs_func = self.deeplift_model.get_target_contribs_func(
            find_scores_layer_idx=self.input_layer_idxs,
            target_layer_idx=output_layer)


    @classmethod
    def is_compatible(cls, model):
        # TODO: implement check for required functions
        # specifically, a "save_in_keras2" func that saves in the keras2
        # format, and also test that the conversion works

        if model.type != "keras":
            # Support only keras models
            return False
        # Check the Keras backend
        import keras
        import keras.backend as K

        if not keras.__version__.startswith("2.0."):
            return False

        # TODO - check the Keras version
        if model.backend is None:
            backend = K.backend()
        else:
            backend = model.backend
        return backend == "tensorflow"

    def score(self, input_batch, input_ref):
        #from deeplift.util import run_function_in_batches
        x_standardized = self.model._batch_to_list(input_batch)
        ref_standaradized = None
        if input_ref is not None:
            ref_standaradized = self.model._batch_to_list(input_ref)

        scores = self.deeplift_contribs_func(task_idx=self.task_idx,
                                                 input_data_list=x_standardized,
                                                 input_references_list = ref_standaradized,
                                                 batch_size=self.batch_size,
                                                 progress_update=1000)

        """
        # run_function_in_batches fails for 
        scores = run_function_in_batches(
            func=self.deeplift_contribs_func,
            input_data_list=x_standardized,
            batch_size=self.batch_size,
            progress_update=1000,
            task_idx=self.task_idx)
        """

        # DeepLIFT returns all samples as a list of individual samples
        scores = [numpy_collate(el) for el in scores]

        # re-format the list-type input back to how the input_batch was:
        scores = self.model._match_to_input(scores, input_batch)
        return scores

    def predict_on_batch(self, input_batch):
        from deeplift.util import run_function_in_batches
        from deeplift.util import compile_func
        x_standardized = self.model._batch_to_list(input_batch)
        if self.fwd_predict_fn is None:
            # identify model output layers:
            self.output_layers_idxs = []
            for output_name in self.model.model.output_names:
                for i, l in enumerate(self.model.model.layers):
                    if l.name == output_name:
                        self.output_layers_idxs.append(i)
            inputs = [self.deeplift_model.get_layers()[i].get_activation_vars() for i in self.input_layer_idxs]
            outputs = [self.deeplift_model.get_layers()[-1].get_activation_vars()]
            deeplift_prediction_func = compile_func(inputs,outputs)

        preds = run_function_in_batches(
            input_data_list=x_standardized,
            func=deeplift_prediction_func,
            batch_size=self.batch_size,
            progress_update=None)

        preds = np.array(preds)
        if len(self.output_layers_idxs) == 1:
            preds = preds[0,...]

        return preds



class IntegratedGradients(ImportanceScoreWRef, Gradient):

    def score(self, input_batch, input_ref):
        grads = super().score(input_batch)
        # TODO implement integrated gradients
        # https://github.com/marcoancona/DeepExplain/blob/master/deepexplain/tensorflow/methods.py#L208-L225
        pass


class GradientXInput(ImportanceScoreWRef, Gradient):
    # AbstractGrads implements the

    def score(self, input_batch, input_ref):
        # TODO - handle also the case where input_ref is not a simple array but a list
        return super().score(input_batch) * input_ref


METHODS = {"deeplift": DeepLift,
           "grad*input": GradientXInput,
           "intgrad": IntegratedGradients}