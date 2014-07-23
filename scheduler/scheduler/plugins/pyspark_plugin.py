import functools
import ujson

from ds_commons import argparse_tools as at
from scheduler import dag_tools, runner
from . import pyspark_context
from ds_commons.log import log


def main(ns, job_id):
    """
    A generic plugin that uses Spark to:
        read data,
        transform data with given code,
        and maybe write transformed data

    Assume code is written in Python.  For Scala or R code, use another option.
    """
    module = dag_tools.get_pymodule(ns.app_name)

    pjob_id = dag_tools.parse_job_id(ns.app_name, job_id)
    read_fp, _ = pyspark_context.get_s3_fp(
        ns, read=True, write=False, **pjob_id)
    log_details = dict(
        module_name=module.__name__, read_fp=read_fp,
        app_name=ns.app_name, job_id=job_id)

    conf, osenv, files, pyFiles = dag_tools.get_spark_conf(ns.app_name)
    conf['spark.app.name'] = "%s__%s" % (conf['spark.app.name'], job_id)
    conf.update(ns.spark_conf)
    sc = pyspark_context.get_spark_context(
        conf=conf, osenv=osenv, files=files, pyFiles=pyFiles)

    tf = sc.textFile(read_fp, ns.minPartitions)
    log.debug('calling module.main(...)')
    tf = pre_process_data(ns=ns, tf=tf, log_details=log_details)
    apply_data_transform(
        ns=ns, tf=tf, log_details=log_details, pjob_id=pjob_id, module=module)
    sc.stop()


def pre_process_data(ns, tf, log_details):
    """
    For convenience, perform operations on the input stream before passing
    along to other data processing code
    """
    if ns.sample:
        log.info('sampling a percentage of input data without replacement',
                 extra=dict(sampling_pct=ns.sample, **log_details))
        tf = tf.sample(False, ns.sample, 0)
    if ns.mapjson:
        log.info('converting all elements in data stream to json')
        tf = tf.map(ujson.loads)
    return tf


def apply_data_transform(ns, tf, log_details, pjob_id, module):
    """Pass control to the module.main method.  If module.main specifies a
    `textFile` parameter, pass the textFile instance.  Otherwise, just map
    module.main on the RDD"""
    func_args = (module.main.func_code
                 .co_varnames[:module.main.func_code.co_argcount])
    if 'textFile' in func_args:
        log.info(
            'passing textFile instance to a module.main function',
            extra=log_details)
        try:
            module.main(textFile=tf, ns=ns, **pjob_id)
        except Exception as err:
            runner.log_and_raise(
                "Job failed with error: %s" % err, log_details)

    else:
        _, write_fp = pyspark_context.get_s3_fp(
            ns, read=False, write=True, **pjob_id)
        log.info(
            'mapping a module.main function to all elements in a textFile and'
            ' writing output', extra=dict(write_fp=write_fp, **log_details))
        try:
            (
                tf
                .map(functools.partial(module.main, ns=ns, **pjob_id))
                .saveAsTextFile(write_fp)
            )
        except Exception as err:
            runner.log_and_raise(err, log_details)


def _validate_sample_size(str_i):
    """Helper for --sample argument option in argparse"""
    i = float(str_i)
    assert 0 <= i <= 1, "given sample size must be a number between [0, 1]"
    return i


_build_arg_parser = runner.build_plugin_arg_parser([at.group(
    'Spark Job Options: How should given module.main process data?',
    at.group(
        "Preprocess data",
        at.add_argument(
            '--mapjson', action='store_true', help=(
                'convert each element in the textFile to json before doing'
                ' anything with it.')),
        at.add_argument(
            '--sample', type=_validate_sample_size,
            help="Sample n percent of the data without replacement"),
    ),
    at.s3_key_bucket(type='write'),
    at.s3_key_bucket(type='read'),
    at.add_argument(
        '--spark_conf', nargs='*',
        type=lambda x: x.split('='), default=[],
        help=("A list of key=value pairs that override"
              " the task's default settings. ie:"
              " spark.master=local[4] spark.ui.port=4046")),
    at.add_argument(
        '--minPartitions', type=int,
        help=("2nd argument passed to textFile")),
)], add_help=False,
)


def build_arg_parser():
    """
    Get the app's parser, have it inherit from this parser
    (which may inherit from other parsers)
    Return the a new parser that inherits from app's parser,
    from this parser, and implicitly from this parser's parents (if it has any)

    I'm doing this so that python spark apps can have a simple, standard design
    """
    parents = [_build_arg_parser()]

    _ns, _ = parents[0].parse_known_args()
    _app = dag_tools.get_pymodule(_ns.app_name)
    if hasattr(_app, 'build_arg_parser'):
        parents.append(
            _app.build_arg_parser()
        )

    return at.build_arg_parser(
        [], parents=parents, conflict_handler='resolve')()