# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run masked LM/next sentence masked_lm pre-training for BERT."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
import time

from tensorflow.python.training.session_run_hook import SessionRunHook

import modeling
import optimization
import tensorflow as tf
import numpy as np
import sys
import pickle
from argparse import ArgumentParser

## Required parameters
parser = ArgumentParser()

parser.add_argument(
    "--bert_config_file", default=None,
    help="The config json file corresponding to the pre-trained BERT model. "
    "This specifies the model architecture.", required=True, 
    type=str)

parser.add_argument(
    "--train_input_file", default=None,
    help="Input TF example files (can be a glob or comma separated).", type=str)

parser.add_argument(
    "--test_input_file", default=None,
    help="Input TF example files (can be a glob or comma separated).", type=str)

parser.add_argument(
    "--checkpointDir", default=None,
    help="The output directory where the model checkpoints will be written.", type=str, required=True)

parser.add_argument("--signature", default='default', help="signature_name", type=str)

## Other parameters
parser.add_argument(
    "--init_checkpoint", default=None,
    help="Initial checkpoint (usually from a pre-trained BERT model).", type=str)

parser.add_argument(
    "--max_seq_length", default=128, type=int,
    help="The maximum total input sequence length after WordPiece tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded. Must match data generation.")

parser.add_argument("--max_predictions_per_seq", default=20, type=int,
                     help="Maximum number of masked LM predictions per sequence. "
                     "Must match data generation.")

parser.add_argument("--do_train", default=False, type=bool, help="Whether to run training.")

parser.add_argument("--changed", default=False, type=bool, help="Whether to run training.")

parser.add_argument("--do_eval", default=False, type=bool, help="Whether to run eval on the dev set.")

#flags.DEFINE_integer("train_batch_size", 32, "Total batch size for training.")
parser.add_argument("--batch_size", default=32, type=bool, help="Total batch size for training.")

#flags.DEFINE_integer("eval_batch_size", 1, "Total batch size for eval.")

parser.add_argument("--learning_rate", default=5e-5, type=float,
                   help="The initial learning rate for Adam.")

parser.add_argument("--num_train_steps", default=100000, type=int, help="Number of training steps.")

parser.add_argument("--num_warmup_steps", default=10000, type=int, help="Number of warmup steps.")

parser.add_argument("--save_checkpoints_steps", default=1000, type=int, help="How often to save the model checkpoint.")

parser.add_argument("--iterations_per_loop", default=1000, type=int, help="How many steps to make in each estimator call.")

parser.add_argument("--max_eval_steps", default=1000, type=int, help="Maximum number of eval steps.")


parser.add_argument("--use_tpu", default=False, type=bool, help="Whether to use TPU or GPU/CPU.")

parser.add_argument(
    "--tpu_name", default=None, type=str, 
    help="The Cloud TPU to use for training. This should be either the name "
    "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
    "url.")

parser.add_argument(
    "--tpu_zone", default=None, type=str,
    help="[Optional] GCE zone where the Cloud TPU is located in. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")

parser.add_argument(
    "--gcp_project", default=None, type=str,
    help="[Optional] Project name for the Cloud TPU-enabled project. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")

parser.add_argument("--master", default=None, type=str, help="[Optional] TensorFlow master URL.")

parser.add_argument(
    "--num_tpu_cores", default=8, type=int,
    help="Only used if `use_tpu` is True. Total number of TPU cores to use.")

parser.add_argument("--use_pop_random", default=True, type=bool, help="use pop random negative samples")
parser.add_argument("--vocab_filename", default=None, type=str, help="vocab filename")
parser.add_argument("--user_history_filename", default=None, type=str, help="user history filename", required=True)
parser.add_argument("--save_predictions_file", default=None, type=str, help="save predictions into file")
parser.add_argument("--predictions_per_user", default=1000, type=int, help="number of predictions to save into file")
parser.add_argument("--training-time-limit-seconds", default=None, type=int, help="training will stop after N seconds")
FLAGS=parser.parse_args()

output_file = None


class EvalHooks(tf.estimator.SessionRunHook):
    def __init__(self):
        tf.compat.v1.logging.info('run init')

    def begin(self):
        self.valid_user = 0.0

        self.ndcg_1 = 0.0
        self.hit_1 = 0.0
        self.ndcg_5 = 0.0
        self.hit_5 = 0.0
        self.ndcg_10 = 0.0
        self.hit_10 = 0.0
        self.ap = 0.0

        np.random.seed(12345)

        self.vocab = None

        if FLAGS.user_history_filename is not None:
            print('load user history from :' + FLAGS.user_history_filename)
            with open(FLAGS.user_history_filename, 'rb') as input_file:
                self.user_history = pickle.load(input_file)

        if FLAGS.vocab_filename is not None:
            print('load vocab from :' + FLAGS.vocab_filename)
            with open(FLAGS.vocab_filename, 'rb') as input_file:
                self.vocab = pickle.load(input_file)

            keys = self.vocab.counter.keys()
            values = self.vocab.counter.values()
            self.ids = self.vocab.convert_tokens_to_ids(keys)
            # normalize
            # print(values)
            sum_value = np.sum([x for x in values])
            # print(sum_value)
            self.probability = [value / sum_value for value in values]

    def end(self, session):
        strin = "ndcg@1:{}, hit@1:{}， ndcg@5:{}, hit@5:{}, ndcg@10:{}, hit@10:{}, ap:{}, valid_user:{}".format(
            self.ndcg_1 / self.valid_user, self.hit_1 / self.valid_user,
            self.ndcg_5 / self.valid_user, self.hit_5 / self.valid_user,
            self.ndcg_10 / self.valid_user,
            self.hit_10 / self.valid_user, self.ap / self.valid_user,
            self.valid_user
        )

        with open("/log_file.txt", "a") as f:
            f.write(strin)
        print(strin)

    def before_run(self, run_context):
        variables = tf.compat.v1.get_collection('eval_sp')
        return tf.estimator.SessionRunArgs(variables)

    def after_run(self, run_context, run_values):
        masked_lm_log_probs, input_ids, masked_lm_ids, info = run_values.results
        masked_lm_log_probs = masked_lm_log_probs.reshape(
            (-1, FLAGS.max_predictions_per_seq, masked_lm_log_probs.shape[1]))

        if (FLAGS.save_predictions_file is not None):
            for idx in range(len(input_ids)):
                user_id = f"user_{info[idx][0]}"
                scores = masked_lm_log_probs[idx, 0]
                predicted_items = np.argsort(scores)[-FLAGS.predictions_per_user:][::-1]
                output_file.write(user_id)
                for item_id in predicted_items:
                    try:
                        token = self.vocab.convert_ids_to_tokens([item_id])[0]
                        score = scores[item_id]
                        output_file.write(f";{token}:{score}")
                    except IndexError:
                        continue
                output_file.write("\n")


        for idx in range(len(input_ids)):
            rated = set(input_ids[idx])
            rated.add(0)
            rated.add(masked_lm_ids[idx][0])
            map(lambda x: rated.add(x),
                self.user_history["user_" + str(info[idx][0])][0])
            item_idx = [masked_lm_ids[idx][0]]
            masked_lm_log_probs_elem = masked_lm_log_probs[idx, 0]
            size_of_prob = len(self.ids) + 1  # len(masked_lm_log_probs_elem)
            if FLAGS.use_pop_random:
                if self.vocab is not None:
                    while len(item_idx) < 101:
                        sampled_ids = np.random.choice(self.ids, 101, replace=False, p=self.probability)
                        sampled_ids = [x for x in sampled_ids if x not in rated and x not in item_idx]
                        item_idx.extend(sampled_ids[:])
                    item_idx = item_idx[:101]
            else:
                for _ in range(100):
                    t = np.random.randint(1, size_of_prob)
                    while t in rated:
                        t = np.random.randint(1, size_of_prob)
                    item_idx.append(t)

            predictions = -masked_lm_log_probs_elem[item_idx]
            rank = predictions.argsort().argsort()[0]

            self.valid_user += 1

            if self.valid_user % 100 == 0:
                print('.', end='')
                sys.stdout.flush()

            if rank < 1:
                self.ndcg_1 += 1
                self.hit_1 += 1
            if rank < 5:
                self.ndcg_5 += 1 / np.log2(rank + 2)
                self.hit_5 += 1
            if rank < 10:
                self.ndcg_10 += 1 / np.log2(rank + 2)
                self.hit_10 += 1

            self.ap += 1.0 / (rank + 1)

class TrainHooks(tf.estimator.SessionRunHook):
    def __init__(self, time_limit):
        self.time_limit = time_limit

    def begin(self):
        self.training_start_time = time.time()

    def after_run(self,
                run_context,  # pylint: disable=unused-argument
                run_values):
        training_time = time.time() - self.training_start_time
        if self.time_limit is not None and training_time >= self.time_limit:
            tf.compat.v1.logging.info(f"time limit: stopping training after {training_time} seconds")
            raise StopIteration()




def model_fn_builder(bert_config, init_checkpoint, learning_rate,
                     num_train_steps, num_warmup_steps, use_tpu,
                     use_one_hot_embeddings, item_size):
    """Returns `model_fn` closure for TPUEstimator."""

    def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
        """The `model_fn` for TPUEstimator."""

        tf.compat.v1.logging.info("*** Features ***")
        for name in sorted(features.keys()):
            tf.compat.v1.logging.info("  name = %s, shape = %s" % (name,
                                                         features[name].shape))

        info = features["info"]
        input_ids = features["input_ids"]
        input_mask = features["input_mask"]
        masked_lm_positions = features["masked_lm_positions"]
        masked_lm_ids = features["masked_lm_ids"]
        masked_lm_weights = features["masked_lm_weights"]

        is_training = (mode == tf.estimator.ModeKeys.TRAIN)

        model = modeling.BertModel(
            config=bert_config,
            is_training=is_training,
            input_ids=input_ids,
            input_mask=input_mask,
            token_type_ids=None,
            use_one_hot_embeddings=use_one_hot_embeddings)
        
#         all_user_and_item = model.get_embedding_table()
#         item_ids = [i for i in range(0, item_size + 1)]
#         softmax_output_embedding = tf.nn.embedding_lookup(all_user_and_item, item_ids)

        (masked_lm_loss,
         masked_lm_example_loss, masked_lm_log_probs) = get_masked_lm_output(
             bert_config,
             model.get_sequence_output(),
             model.get_embedding_table(), masked_lm_positions, masked_lm_ids,
             masked_lm_weights)

        total_loss = masked_lm_loss

        tvars = tf.compat.v1.trainable_variables()

        initialized_variable_names = {}
        scaffold_fn = None
        if init_checkpoint:
            (assignment_map, initialized_variable_names
             ) = modeling.get_assignment_map_from_checkpoint(
                 tvars, init_checkpoint)
            if use_tpu:

                def tpu_scaffold():
                    tf.compat.v1.train.init_from_checkpoint(init_checkpoint,
                                                  assignment_map)
                    return tf.compat.v1.train.Scaffold()

                scaffold_fn = tpu_scaffold
            else:
                tf.compat.v1.train.init_from_checkpoint(init_checkpoint, assignment_map)

        tf.compat.v1.logging.info("**** Trainable Variables ****")
        for var in tvars:
            init_string = ""
            if var.name in initialized_variable_names:
                init_string = ", *INIT_FROM_CKPT*"
            tf.compat.v1.logging.info("  name = %s, shape = %s%s", var.name, var.shape,
                            init_string)

        output_spec = None
        if mode == tf.estimator.ModeKeys.TRAIN:
            train_op = optimization.create_optimizer(total_loss, learning_rate,
                                                     num_train_steps,
                                                     num_warmup_steps, use_tpu, changed=FLAGS.changed)

            output_spec = tf.estimator.EstimatorSpec(
                mode=mode,
                loss=total_loss,
                train_op=train_op,
                scaffold=scaffold_fn)
        elif mode == tf.estimator.ModeKeys.EVAL:

            def metric_fn(masked_lm_example_loss, masked_lm_log_probs,
                          masked_lm_ids, masked_lm_weights):
                """Computes the loss and accuracy of the model."""
                masked_lm_log_probs = tf.reshape(
                    masked_lm_log_probs, [-1, masked_lm_log_probs.shape[-1]])
                masked_lm_predictions = tf.argmax(
                    input=masked_lm_log_probs, axis=-1, output_type=tf.int32)
                masked_lm_example_loss = tf.reshape(masked_lm_example_loss,
                                                    [-1])
                masked_lm_ids = tf.reshape(masked_lm_ids, [-1])
                masked_lm_weights = tf.reshape(masked_lm_weights, [-1])
                masked_lm_accuracy = tf.compat.v1.metrics.accuracy(
                    labels=masked_lm_ids,
                    predictions=masked_lm_predictions,
                    weights=masked_lm_weights)
                masked_lm_mean_loss = tf.compat.v1.metrics.mean(
                    values=masked_lm_example_loss, weights=masked_lm_weights)

                return {
                    "masked_lm_accuracy": masked_lm_accuracy,
                    "masked_lm_loss": masked_lm_mean_loss,
                }

            tf.compat.v1.add_to_collection('eval_sp', masked_lm_log_probs)
            tf.compat.v1.add_to_collection('eval_sp', input_ids)
            tf.compat.v1.add_to_collection('eval_sp', masked_lm_ids)
            tf.compat.v1.add_to_collection('eval_sp', info)

            eval_metrics = metric_fn(masked_lm_example_loss,
                                     masked_lm_log_probs, masked_lm_ids,
                                     masked_lm_weights)
            output_spec = tf.estimator.EstimatorSpec(
                mode=mode,
                loss=total_loss,
                eval_metric_ops=eval_metrics,
                scaffold=scaffold_fn)
        else:
            raise ValueError("Only TRAIN and EVAL modes are supported: %s" %
                             (mode))

        return output_spec

    return model_fn


def get_masked_lm_output(bert_config, input_tensor, output_weights, positions,
                         label_ids, label_weights):
    """Get loss and log probs for the masked LM."""
    # [batch_size*label_size, dim]
    input_tensor = gather_indexes(input_tensor, positions)

    with tf.compat.v1.variable_scope("cls/predictions"):
        # We apply one more non-linear transformation before the output layer.
        # This matrix is not used after pre-training.
        with tf.compat.v1.variable_scope("transform"):
            input_tensor = tf.compat.v1.layers.dense(
                input_tensor,
                units=bert_config.hidden_size,
                activation=modeling.get_activation(bert_config.hidden_act),
                kernel_initializer=modeling.create_initializer(
                    bert_config.initializer_range))
            input_tensor = modeling.layer_norm(input_tensor)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        output_bias = tf.compat.v1.get_variable(
            "output_bias",
            shape=[output_weights.shape[0]],
            initializer=tf.compat.v1.zeros_initializer())
        logits = tf.matmul(input_tensor, output_weights, transpose_b=True)
        logits = tf.nn.bias_add(logits, output_bias)
        # logits, (bs*label_size, vocab_size)
        log_probs = tf.nn.log_softmax(logits, -1)

        label_ids = tf.reshape(label_ids, [-1])
        label_weights = tf.reshape(label_weights, [-1])

        one_hot_labels = tf.one_hot(
            label_ids, depth=output_weights.shape[0], dtype=tf.float32)

        # The `positions` tensor might be zero-padded (if the sequence is too
        # short to have the maximum number of predictions). The `label_weights`
        # tensor has a value of 1.0 for every real prediction and 0.0 for the
        # padding predictions.
        per_example_loss = -tf.reduce_sum(
            input_tensor=log_probs * one_hot_labels, axis=[-1])
        numerator = tf.reduce_sum(input_tensor=label_weights * per_example_loss)
        denominator = tf.reduce_sum(input_tensor=label_weights) + 1e-5
        loss = numerator / denominator

    return (loss, per_example_loss, log_probs)


def gather_indexes(sequence_tensor, positions):
    """Gathers the vectors at the specific positions over a minibatch."""
    sequence_shape = modeling.get_shape_list(sequence_tensor, expected_rank=3)
    batch_size = sequence_shape[0]
    seq_length = sequence_shape[1]
    width = sequence_shape[2]

    flat_offsets = tf.reshape(
        tf.range(0, batch_size, dtype=tf.int32) * seq_length, [-1, 1])
    flat_positions = tf.reshape(positions + flat_offsets, [-1])
    flat_sequence_tensor = tf.reshape(sequence_tensor,
                                      [batch_size * seq_length, width])
    output_tensor = tf.gather(flat_sequence_tensor, flat_positions)
    return output_tensor


def input_fn_builder(input_files,
                     max_seq_length,
                     max_predictions_per_seq,
                     is_training,
                     num_cpu_threads=4):
    """Creates an `input_fn` closure to be passed to TPUEstimator."""

    def input_fn(params):
        """The actual input function."""
        batch_size = params["batch_size"]

        name_to_features = {
            "info":
            tf.io.FixedLenFeature([1], tf.int64),  #[user]
            "input_ids":
            tf.io.FixedLenFeature([max_seq_length], tf.int64),
            "input_mask":
            tf.io.FixedLenFeature([max_seq_length], tf.int64),
            "masked_lm_positions":
            tf.io.FixedLenFeature([max_predictions_per_seq], tf.int64),
            "masked_lm_ids":
            tf.io.FixedLenFeature([max_predictions_per_seq], tf.int64),
            "masked_lm_weights":
            tf.io.FixedLenFeature([max_predictions_per_seq], tf.float32)
        }

        # For training, we want a lot of parallel reading and shuffling.
        # For eval, we want no shuffling and parallel reading doesn't matter.
        if is_training:
            d = tf.data.TFRecordDataset(input_files)
            d = d.repeat()
            d = d.shuffle(buffer_size=100)

            # `cycle_length` is the number of parallel files that get read.
            #cycle_length = min(num_cpu_threads, len(input_files))

            # `sloppy` mode means that the interleaving is not exact. This adds
            # even more randomness to the training pipeline.
            #d = d.apply(
            #    tf.contrib.data.parallel_interleave(
            #        tf.data.TFRecordDataset,
            #        sloppy=is_training,
            #        cycle_length=cycle_length))
            #d = d.shuffle(buffer_size=100)
        else:
            d = tf.data.TFRecordDataset(input_files)


        d = d.map(
            lambda record: _decode_record(record, name_to_features),
            num_parallel_calls=num_cpu_threads)
        d = d.batch(batch_size=batch_size)
        return d

    return input_fn


def _decode_record(record, name_to_features):
    """Decodes a record to a TensorFlow example."""
    example = tf.io.parse_single_example(serialized=record, features=name_to_features)

    # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
    # So cast all int64 to int32.
    for name in list(example.keys()):
        t = example[name]
        if t.dtype == tf.int64:
            t = tf.cast(t, dtype=tf.int32)
        example[name] = t

    return example


def main(_):
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.INFO)

    FLAGS.checkpointDir = FLAGS.checkpointDir + FLAGS.signature
    print('checkpointDir:', FLAGS.checkpointDir)

    if not FLAGS.do_train and not FLAGS.do_eval:
        raise ValueError(
            "At least one of `do_train` or `do_eval` must be True.")

    bert_config = modeling.BertConfig.from_json_file(FLAGS.bert_config_file)

    tf.io.gfile.makedirs(FLAGS.checkpointDir)

    train_input_files = []
    for input_pattern in FLAGS.train_input_file.split(","):
        train_input_files.extend(tf.io.gfile.glob(input_pattern))

    test_input_files = []
    if FLAGS.test_input_file is None:
        test_input_files = train_input_files
    else:
        for input_pattern in FLAGS.test_input_file.split(","):
            test_input_files.extend(tf.io.gfile.glob(input_pattern))

    tf.compat.v1.logging.info("*** train Input Files ***")
    for input_file in train_input_files:
        tf.compat.v1.logging.info("  %s" % input_file)

    tf.compat.v1.logging.info("*** test Input Files ***")
    for input_file in train_input_files:
        tf.compat.v1.logging.info("  %s" % input_file)

    tpu_cluster_resolver = None

    #is_per_host = tf.contrib.tpu.InputPipelineConfig.PER_HOST_V2
    run_config = tf.estimator.RunConfig(
        model_dir=FLAGS.checkpointDir,
        save_checkpoints_steps=FLAGS.save_checkpoints_steps)
    
    if FLAGS.vocab_filename is not None:
        with open(FLAGS.vocab_filename, 'rb') as input_file:
            vocab = pickle.load(input_file)
    item_size = len(vocab.counter)

    model_fn = model_fn_builder(
        bert_config=bert_config,
        init_checkpoint=FLAGS.init_checkpoint,
        learning_rate=FLAGS.learning_rate,
        num_train_steps=FLAGS.num_train_steps,
        num_warmup_steps=FLAGS.num_warmup_steps,
        use_tpu=FLAGS.use_tpu,
        use_one_hot_embeddings=FLAGS.use_tpu,
        item_size=item_size)

    # If TPU is not available, this will fall back to normal Estimator on CPU
    # or GPU.
    estimator = tf.estimator.Estimator(
        model_fn=model_fn,
        config=run_config,
        params={
            "batch_size": FLAGS.batch_size
        })

    if FLAGS.do_train:
        tf.compat.v1.logging.info("***** Running training *****")
        tf.compat.v1.logging.info("  Batch size = %d", FLAGS.batch_size)
        train_input_fn = input_fn_builder(
            input_files=train_input_files,
            max_seq_length=FLAGS.max_seq_length,
            max_predictions_per_seq=FLAGS.max_predictions_per_seq,
            is_training=True)
        estimator.train(
            input_fn=train_input_fn, max_steps=FLAGS.num_train_steps,
                     hooks=[TrainHooks(FLAGS.training_time_limit_seconds)])

    if FLAGS.do_eval:
        tf.compat.v1.logging.info("***** Running evaluation *****")
        tf.compat.v1.logging.info("  Batch size = %d", FLAGS.batch_size)

        eval_input_fn = input_fn_builder(
            input_files=test_input_files,
            max_seq_length=FLAGS.max_seq_length,
            max_predictions_per_seq=FLAGS.max_predictions_per_seq,
            is_training=False)

        #tf.logging.info('special eval ops:', special_eval_ops)
        result = estimator.evaluate(
            input_fn=eval_input_fn,
            steps=None,
            hooks=[EvalHooks()])

        output_eval_file = os.path.join(FLAGS.checkpointDir,
                                        "eval_results.txt")
        with tf.io.gfile.GFile(output_eval_file, "w") as writer:
            tf.compat.v1.logging.info("***** Eval results *****")
            tf.compat.v1.logging.info(bert_config.to_json_string())
            writer.write(bert_config.to_json_string()+'\n')
            for key in sorted(result.keys()):
                tf.compat.v1.logging.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))


if __name__ == "__main__":
    if FLAGS.save_predictions_file is not None:
        output_file = open(FLAGS.save_predictions_file, "w")

    physical_devices = tf.config.list_physical_devices('GPU')
    tf.config.experimental.set_memory_growth(physical_devices[0], True)
    tf.compat.v1.app.run()
