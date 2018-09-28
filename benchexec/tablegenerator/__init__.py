# BenchExec is a framework for reliable benchmarking.
# This file is part of BenchExec.
#
# Copyright (C) 2007-2015  Dirk Beyer
# All rights reserved.
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

# prepare for Python 3
from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import bz2
import collections
import copy
from decimal import Decimal, InvalidOperation
import gzip
import io
import itertools
import logging
import os.path
import signal
import subprocess
import sys
import time
import math
import urllib.parse
import urllib.request
from xml.etree import ElementTree

import tempita
from functools import reduce

from benchexec import __version__
import benchexec.result as result
from benchexec.tablegenerator import util as Util
from benchexec.tablegenerator.columns import Column, ColumnType, get_column_type
import zipfile

# Process pool for parallel work.
# Some of our loops are CPU-bound (e.g., statistics calculations), thus we use
# processes, not threads.
# Fully initialized only in main() because we cannot do so in the worker processes.
parallel = Util.DummyExecutor()

# Most important columns that should be shown first in tables (in the given order)
MAIN_COLUMNS = ['status', 'category', 'cputime', 'walltime', 'memUsage', 'cpuenergy']

NAME_START = "results" # first part of filename of table

DEFAULT_OUTPUT_PATH = "results/"

LIB_URL = "https://cdn.jsdelivr.net"
LIB_URL_OFFLINE = "lib/javascript"

TEMPLATE_FILE_NAME = os.path.join(os.path.dirname(__file__), 'template.{format}')
TEMPLATE_FORMATS = ['html', 'csv']
TEMPLATE_ENCODING = 'UTF-8'
TEMPLATE_NAMESPACE={
   'flatten': Util.flatten,
   'json': Util.to_json,
   'create_link': Util.create_link,
   'format_options': Util.format_options,
   }

_BYTE_FACTOR = 1000 # bytes in a kilobyte

UNIT_CONVERSION = {
    's': {'ms': 1000, 'min': 1.0/60, 'h': 1.0/3600},
    'B': {'kB': 1.0/10**3, 'MB': 1.0/10**6, 'GB': 1.0/10**9},
    'J': {'kJ': 1.0/10**3, 'Ws': 1, 'kWs': 1.0/1000,
          'Wh': 1.0/3600, 'kWh': 1.0/(1000*3600), 'mWh': 1.0/(1000*1000*3600)}
}

nan = float('nan')
inf = float('inf')


def parse_table_definition_file(file):
    '''
    Read an parse the XML of a table-definition file.
    @return: an ElementTree object for the table definition
    '''
    logging.info("Reading table definition from '%s'...", file)
    if not os.path.isfile(file):
        logging.error("File '%s' does not exist.", file)
        exit(1)

    try:
        tableGenFile = ElementTree.ElementTree().parse(file)
    except IOError as e:
        logging.error('Could not read result file %s: %s', file, e)
        exit(1)
    except ElementTree.ParseError as e:
        logging.error('Table file %s is invalid: %s', file, e)
        exit(1)
    if 'table' != tableGenFile.tag:
        logging.error("Table file %s is invalid: It's root element is not named 'table'.", file)
        exit(1)
    return tableGenFile


def table_definition_lists_result_files(table_definition):
    return any(tag.tag in ['result', 'union'] for tag in table_definition)


def load_results_from_table_definition(table_definition, table_definition_file, options):
    """
    Load all results in files that are listed in the given table-definition file.
    @return: a list of RunSetResult objects
    """
    default_columns = extract_columns_from_table_definition_file(table_definition, table_definition_file)
    columns_relevant_for_diff = _get_columns_relevant_for_diff(default_columns)

    results = []
    for tag in table_definition:
        if tag.tag == 'result':
            columns = extract_columns_from_table_definition_file(tag, table_definition_file) or default_columns
            run_set_id = tag.get('id')
            for resultsFile in get_file_list_from_result_tag(tag, table_definition_file):
                results.append(parallel.submit(
                    load_result, resultsFile, options, run_set_id, columns, columns_relevant_for_diff))

        elif tag.tag == 'union':
            results.append(parallel.submit(
                handle_union_tag, tag, table_definition_file, options, default_columns, columns_relevant_for_diff))

    return [future.result() for future in results]


def handle_union_tag(tag, table_definition_file, options, default_columns, columns_relevant_for_diff):
    columns = extract_columns_from_table_definition_file(tag, table_definition_file) or default_columns
    result = RunSetResult([], collections.defaultdict(list), columns)

    for resultTag in tag.findall('result'):
        run_set_id = resultTag.get('id')
        for resultsFile in get_file_list_from_result_tag(resultTag, table_definition_file):
            result_xml = parse_results_file(resultsFile, run_set_id)
            if result_xml is not None:
                result.append(resultsFile, result_xml, options.all_columns)

    if not result._xml_results:
        return None

    name = tag.get('title', tag.get('name'))
    if name:
        result.attributes['name'] = [name]
    result.collect_data(options.correct_only)
    return result


def get_file_list_from_result_tag(result_tag, table_definition_file):
    if not 'filename' in result_tag.attrib:
        logging.warning("Result tag without filename attribute in file '%s'.", table_definition_file)
        return []
    return Util.get_file_list(os.path.join(os.path.dirname(table_definition_file), result_tag.get('filename'))) # expand wildcards


def load_results_with_table_definition(result_files, table_definition, table_definition_file, options):
    """
    Load results from given files with column definitions taken from a table-definition file.
    @return: a list of RunSetResult objects
    """
    columns = extract_columns_from_table_definition_file(table_definition, table_definition_file)
    columns_relevant_for_diff = _get_columns_relevant_for_diff(columns)

    return load_results(
        result_files,
        options=options,
        columns=columns,
        columns_relevant_for_diff=columns_relevant_for_diff)


def extract_columns_from_table_definition_file(xmltag, table_definition_file):
    """
    Extract all columns mentioned in the result tag of a table definition file.
    """
    def handle_path(path):
        """Convert path from a path relative to table-definition file."""
        if not path or path.startswith("http://") or path.startswith("https://"):
            return path
        return os.path.join(os.path.dirname(table_definition_file), path)

    columns = list()
    for c in xmltag.findall('column'):
        scale_factor = c.get("scaleFactor")
        display_unit = c.get("displayUnit")
        source_unit = c.get("sourceUnit")

        new_column = Column(c.get("title"), c.text, c.get("numberOfDigits"),
                   handle_path(c.get("href")), None, display_unit, source_unit,
                   scale_factor, c.get("relevantForDiff"), c.get("displayTitle"))
        columns.append(new_column)

    return columns


def _get_columns_relevant_for_diff(columns_to_show):
    """
    Extract columns that are relevant for the diff table.

    @param columns_to_show: (list) A list of columns that should be shown
    @return: (set) Set of columns that are relevant for the diff table. If
             none is marked relevant, the column named "status" will be
             returned in the set.
    """
    cols = set([col.title for col in columns_to_show if col.relevant_for_diff])
    if len(cols) == 0:
        return set(
            [col.title for col in columns_to_show if col.title == "status"])
    else:
        return cols


def get_task_id(task, base_path_or_url):
    """
    Return a unique identifier for a given task.
    @param task: the XML element that represents a task
    @return a tuple with filename of task as first element
    """
    name = task.get('name')
    if base_path_or_url:
        if Util.is_url(base_path_or_url):
            name = urllib.parse.urljoin(base_path_or_url, name)
        else:
            name = os.path.normpath(os.path.join(os.path.dirname(base_path_or_url), name))
    task_id = [name,
               task.get('properties'),
               task.get('runset'),
               ]
    return tuple(task_id)


loaded_tools = {}


def load_tool(result):
    """
    Load the module with the tool-specific code.
    """
    def load_tool_module(tool_module):
        if not tool_module:
            logging.warning('Cannot extract values from log files for benchmark results %s '
                            '(missing attribute "toolmodule" on tag "result").',
                            Util.prettylist(result.attributes['name']))
            return None
        try:
            logging.debug('Loading %s', tool_module)
            return __import__(tool_module, fromlist=['Tool']).Tool()
        except ImportError as ie:
            logging.warning(
                'Missing module "%s", cannot extract values from log files (ImportError: %s).',
                tool_module, ie)
        except AttributeError:
            logging.warning(
                'The module "%s" does not define the necessary class Tool, '
                'cannot extract values from log files.',
                tool_module)
        return None

    tool_module = result.attributes['toolmodule'][0] if 'toolmodule' in result.attributes else None
    if tool_module in loaded_tools:
        return loaded_tools[tool_module]
    else:
        result = load_tool_module(tool_module)
        loaded_tools[tool_module] = result
        return result


class RunSetResult(object):
    """
    The Class RunSetResult contains all the results of one execution of a run set:
    the sourcefiles tags (with sourcefiles + values), the columns to show
    and the benchmark attributes.
    """
    def __init__(self, xml_results, attributes, columns, summary={},
                 columns_relevant_for_diff=set()):
        self._xml_results = xml_results
        self.attributes = attributes
        self.columns = copy.deepcopy(columns)  # Copy the columns since they may be modified
        self.summary = summary
        self.columns_relevant_for_diff = columns_relevant_for_diff

    def get_tasks(self):
        """
        Return the list of task ids for these results.
        May be called only after collect_data()
        """
        return [r.task_id for r in self.results]

    def append(self, resultFile, resultElem, all_columns=False):
        """
        Append the result for one run. Needs to be called before collect_data().
        """
        self._xml_results += [(result, resultFile) for result in _get_run_tags_from_xml(resultElem)]
        for attrib, values in RunSetResult._extract_attributes_from_result(resultFile, resultElem).items():
            self.attributes[attrib].extend(values)

        if not self.columns:
            self.columns = RunSetResult._extract_existing_columns_from_result(resultFile, resultElem, all_columns)

    def collect_data(self, correct_only):
        """
        Load the actual result values from the XML file and the log files.
        This may take some time if many log files have to be opened and parsed.
        """
        self.results = []

        def get_value_from_logfile(lines, identifier):
            """
            This method searches for values in lines of the content.
            It uses a tool-specific method to so.
            """
            return load_tool(self).get_value_from_output(lines, identifier)

        # Opening the ZIP archive with the logs for every run is too slow, we cache it.
        log_zip_cache = {}
        try:
            for xml_result, result_file in self._xml_results:
                self.results.append(RunResult.create_from_xml(
                    xml_result, get_value_from_logfile, self.columns,
                    correct_only, log_zip_cache, self.columns_relevant_for_diff, result_file))
        finally:
            for file in log_zip_cache.values():
                file.close()

        for column in self.columns:
            column_values = (run_result.values[run_result.columns.index(column)] for run_result in self.results)
            column.type, column.unit, column.source_unit, column.scale_factor = get_column_type(column, column_values)


        del self._xml_results

    @staticmethod
    def create_from_xml(resultFile, resultElem, columns=None,
                        all_columns=False, columns_relevant_for_diff=set()):
        '''
        This function extracts everything necessary for creating a RunSetResult object
        from the "result" XML tag of a benchmark result file.
        It returns a RunSetResult object, which is not yet fully initialized.
        To finish initializing the object, call collect_data()
        before using it for anything else
        (this is to separate the possibly costly collect_data() call from object instantiation).
        '''
        attributes = RunSetResult._extract_attributes_from_result(resultFile, resultElem)

        if not columns:
            columns = RunSetResult._extract_existing_columns_from_result(resultFile, resultElem, all_columns)

        summary = RunSetResult._extract_summary_from_result(resultElem, columns)

        return RunSetResult([(result, resultFile) for result in _get_run_tags_from_xml(resultElem)],
                attributes, columns, summary, columns_relevant_for_diff)

    @staticmethod
    def _extract_existing_columns_from_result(resultFile, resultElem, all_columns):
        run_results = _get_run_tags_from_xml(resultElem)
        if not run_results:
            logging.warning("Result file '%s' is empty.", resultFile)
            return []
        else: # show all available columns
            column_names = set(
                c.get('title') for s in run_results for c in s.findall('column')
                    if all_columns or c.get('hidden') != 'true')

            # Put main columns first, then rest sorted alphabetically
            columns = ([column for column in MAIN_COLUMNS if column in column_names] +
                       sorted(column_names.difference(MAIN_COLUMNS)))
            return [Column(title, None, None, None) for title in columns]

    @staticmethod
    def _extract_attributes_from_result(resultFile, resultTag):
        attributes = collections.defaultdict(list)

        # Defaults
        attributes['filename'] = [resultFile]
        attributes['branch'] = [os.path.basename(resultFile).split('#')[0] if '#' in resultFile else '']
        attributes['timelimit'] = ['-']
        attributes['memlimit'] = ['-']
        attributes['cpuCores'] = ['-']

        # Update with real values
        for attrib, value in resultTag.attrib.items():
            attributes[attrib] = [value]

        # Add system information if present
        for systemTag in sorted(resultTag.findall('systeminfo'),
                                key=lambda systemTag: systemTag.get('hostname', 'unknown')):
            cpuTag = systemTag.find('cpu')
            attributes['os'   ].append(systemTag.find('os').get('name'))
            attributes['cpu'  ].append(cpuTag.get('model'))
            attributes['cores'].append( cpuTag.get('cores'))
            attributes['freq' ].append(cpuTag.get('frequency'))
            attributes['turbo'].append(cpuTag.get('turboboostActive'))
            attributes['ram'  ].append(systemTag.find('ram').get('size'))
            attributes['host' ].append(systemTag.get('hostname', 'unknown'))

        return attributes

    @staticmethod
    def _extract_summary_from_result(resultTag, columns):
        summary = collections.defaultdict(list)

        # Add summary for columns if present
        for column in resultTag.findall('column'):
            title = column.get('title')
            if title in (c.title for c in columns):
                summary[title] = column.get('value')

        return summary


def _get_run_tags_from_xml(result_elem):
    return result_elem.findall('run') + result_elem.findall('sourcefile')


def load_results(result_files, options, run_set_id=None, columns=None,
                 columns_relevant_for_diff=set()):
    """Version of load_result for multiple input files that will be loaded concurrently."""
    return parallel.map(
        load_result,
        result_files,
        itertools.repeat(options),
        itertools.repeat(run_set_id),
        itertools.repeat(columns),
        itertools.repeat(columns_relevant_for_diff))

def load_result(result_file, options, run_set_id=None, columns=None,
                columns_relevant_for_diff=set()):
    """
    Completely handle loading a single result file.
    @param result_file the file to parse
    @param options additional options
    @param run_set_id the identifier of the run set
    @param columns the list of columns
    @param columns_relevant_for_diff a set of columns that is relevant for
                                     the diff table
    @return a fully ready RunSetResult instance or None
    """
    xml = parse_results_file(result_file, run_set_id=run_set_id, ignore_errors=options.ignore_errors)
    if xml is None:
        return None

    result = RunSetResult.create_from_xml(
        result_file, xml, columns=columns, all_columns=options.all_columns,
        columns_relevant_for_diff=columns_relevant_for_diff)
    result.collect_data(options.correct_only)
    return result


def parse_results_file(resultFile, run_set_id=None, ignore_errors=False):
    '''
    This function parses an XML file that contains the results of the execution of a run set.
    It returns the "result" XML tag.
    @param resultFile: The file name of the XML file that contains the results.
    @param run_set_id: An optional identifier of this set of results.
    '''
    logging.info('    %s', resultFile)
    url = Util.make_url(resultFile)

    parse = ElementTree.ElementTree().parse
    try:
        with Util.open_url_seekable(url, mode='rb') as f:
            try:
                try:
                    resultElem = parse(gzip.GzipFile(fileobj=f))
                except IOError:
                    f.seek(0)
                    try:
                        resultElem = parse(bz2.BZ2File(f))
                    except TypeError:
                        # Python 3.2 does not support giving a file-like object to BZ2File
                        resultElem = parse(io.BytesIO(bz2.decompress(f.read())))
            except IOError:
                f.seek(0)
                resultElem = parse(f)
    except IOError as e:
        logging.error('Could not read result file %s: %s', resultFile, e)
        exit(1)
    except ElementTree.ParseError as e:
        logging.error('Result file %s is invalid: %s', resultFile, e)
        exit(1)

    if resultElem.tag not in ['result', 'test']:
        logging.error("XML file with benchmark results seems to be invalid.\n"
                      "The root element of the file is not named 'result' or 'test'.\n"
                      "If you want to run a table-definition file,\n"
                      "you should use the option '-x' or '--xml'.")
        exit(1)

    if ignore_errors and 'error' in resultElem.attrib:
        logging.warning('Ignoring file "%s" because of error: %s',
                        resultFile,
                        resultElem.attrib['error'])
        return None

    if run_set_id is not None:
        for sourcefile in _get_run_tags_from_xml(resultElem):
            sourcefile.set('runset', run_set_id)

    insert_logfile_names(resultFile, resultElem)
    return resultElem


def insert_logfile_names(resultFile, resultElem):
    # get folder of logfiles (truncate end of XML file name and append .logfiles instead)
    log_folder = resultFile[0:resultFile.rfind('.results.')] + '.logfiles/'

    # append begin of filename
    runSetName = resultElem.get('name')
    if runSetName is not None:
        blockname = resultElem.get('block')
        if blockname is None:
            log_folder += runSetName + "."
        elif blockname == runSetName:
            pass # real runSetName is empty
        else:
            assert runSetName.endswith("." + blockname)
            runSetName = runSetName[:-(1 + len(blockname))] # remove last chars
            log_folder += runSetName + "."

    # for each file: append original filename and insert log_file_name into sourcefileElement
    for sourcefile in _get_run_tags_from_xml(resultElem):
        if 'logfile' in sourcefile.attrib:
            log_file = urllib.parse.urljoin(resultFile, sourcefile.get('logfile'))
        else:
            log_file = log_folder + os.path.basename(sourcefile.get('name')) + ".log"
        sourcefile.set('logfile', log_file)


def merge_tasks(runset_results):
    """
    This function merges the results of all RunSetResult objects.
    If necessary, it can merge lists of names: [A,C] + [A,B] --> [A,B,C]
    and add dummy elements to the results.
    It also ensures the same order of tasks.
    """
    task_list = []
    task_set = set()
    for runset in runset_results:
        index = -1
        currentresult_taskset = set()
        for task in runset.get_tasks():
            if task in currentresult_taskset:
                logging.warning("Task '%s' is present twice, skipping it.", task[0])
            else:
                currentresult_taskset.add(task)
                if task not in task_set:
                    task_list.insert(index+1, task)
                    task_set.add(task)
                    index += 1
                else:
                    index = task_list.index(task)

    merge_task_lists(runset_results, task_list)


def merge_task_lists(runset_results, tasks):
    """
    Set the filelists of all RunSetResult elements so that they contain the same files
    in the same order. For missing files a dummy element is inserted.
    """
    for runset in runset_results:
        # create mapping from id to RunResult object
        # Use reversed list such that the first instance of equal tasks end up in dic
        dic = dict([(run_result.task_id, run_result) for run_result in reversed(runset.results)])
        runset.results = [] # clear and repopulate results
        for task in tasks:
            run_result = dic.get(task)
            if run_result is None:
                logging.info("    No result for task '%s' in '%s'.",
                             task[0], Util.prettylist(runset.attributes['filename']))
                # create an empty dummy element
                run_result = RunResult(task, None, result.CATEGORY_MISSING, 0, None,
                                       runset.columns, [None]*len(runset.columns))
            runset.results.append(run_result)


def find_common_tasks(runset_results):
    tasks_in_first_runset = runset_results[0].get_tasks()

    task_set = set(tasks_in_first_runset)
    for result in runset_results:
        task_set = task_set & set(result.get_tasks())

    task_list = []
    if not task_set:
        logging.warning('No tasks are present in all benchmark results.')
    else:
        task_list = [task for task in tasks_in_first_runset if task in task_set]
        merge_task_lists(runset_results, task_list)


class RunResult(object):
    """
    The class RunResult contains the results of a single verification run.
    """
    def __init__(self, task_id, status, category, score, log_file, columns,
                 values, columns_relevant_for_diff=set(), sourcefiles_exist=True):
        assert(len(columns) == len(values))
        self.task_id = task_id
        self.sourcefiles_exist = sourcefiles_exist
        self.status = status
        self.log_file = log_file
        self.columns = columns
        self.values = values
        self.category = category
        self.score = score
        self.columns_relevant_for_diff = columns_relevant_for_diff

    @staticmethod
    def create_from_xml(sourcefileTag, get_value_from_logfile, listOfColumns,
                        correct_only, log_zip_cache, columns_relevant_for_diff,
                        result_file_or_url):
        '''
        This function collects the values from one run.
        Only columns that should be part of the table are collected.
        '''

        def read_logfile_lines(log_file):
            if not log_file:
                return []
            log_file_url = Util.make_url(log_file)
            url_parts = urllib.parse.urlparse(log_file_url, allow_fragments=False)
            log_zip_path = os.path.dirname(url_parts.path) + ".zip"
            log_zip_url = urllib.parse.urlunparse((url_parts.scheme, url_parts.netloc,
                log_zip_path, url_parts.params, url_parts.query, url_parts.fragment))
            path_in_zip = urllib.parse.unquote(
                os.path.relpath(url_parts.path, os.path.dirname(log_zip_path)))
            if log_zip_url.startswith("file:///") and not log_zip_path.startswith("/"):
                # Replace file:/// with file: for relative paths,
                # otherwise opening fails.
                log_zip_url = "file:" + log_zip_url[8:]

            try:
                with Util.open_url_seekable(log_file_url, 'rt') as logfile:
                    return logfile.readlines()
            except IOError as unused_e1:
                try:
                    if log_zip_url not in log_zip_cache:
                        log_zip_cache[log_zip_url] = zipfile.ZipFile(
                            Util.open_url_seekable(log_zip_url, 'rb'))
                    log_zip = log_zip_cache[log_zip_url]

                    try:
                        with io.TextIOWrapper(log_zip.open(path_in_zip)) as logfile:
                            return logfile.readlines()
                    except KeyError:
                        logging.warning("Could not find logfile '%s' in archive '%s'.",
                                        log_file, log_zip_url)
                        return []

                except IOError as unused_e2:
                    logging.warning("Could not find logfile '%s' nor log archive '%s'.",
                                    log_file, log_zip_url)
                    return []

        status = Util.get_column_value(sourcefileTag, 'status', '')
        category = Util.get_column_value(sourcefileTag, 'category', result.CATEGORY_MISSING)
        score = result.score_for_task(sourcefileTag.get('name'),
                                      sourcefileTag.get('properties', '').split(),
                                      category,
                                      status)
        logfileLines = None

        values = []

        for column in listOfColumns: # for all columns that should be shown
            value = None # default value
            if column.title.lower() == 'score':
                value = str(score)
            elif column.title.lower() == 'status':
                value = status

            elif not correct_only or category == result.CATEGORY_CORRECT:
                if not column.pattern or column.href:
                    # collect values from XML
                    value = Util.get_column_value(sourcefileTag, column.title)

                else: # collect values from logfile
                    if logfileLines is None: # cache content
                        logfileLines = read_logfile_lines(sourcefileTag.get('logfile'))

                    value = get_value_from_logfile(logfileLines, column.pattern)

            values.append(value)

        sourcefiles = sourcefileTag.get('files')
        if sourcefiles:
            if sourcefiles.startswith('['):
                sourcefileList = [s.strip() for s in sourcefiles[1:-1].split(',') if s.strip()]
                sourcefiles_exist = True if sourcefileList else False
            else:
                raise AssertionError('Unknown format for files tag:')
        else:
            sourcefiles_exist = False

        return RunResult(get_task_id(sourcefileTag, result_file_or_url if sourcefiles_exist else None),
                         status, category, score,
                         sourcefileTag.get('logfile'), listOfColumns, values,
                         columns_relevant_for_diff, sourcefiles_exist=sourcefiles_exist)


class Row(object):
    """
    The class Row contains all the results for one sourcefile (a list of RunResult instances).
    It is identified by the name of the source file and optional additional data
    (such as the property).
    It corresponds to one complete row in the final tables.
    """
    def __init__(self, results):
        assert results
        self.results = results
        self.id = results[0].task_id
        self.has_sourcefile = results[0].sourcefiles_exist
        assert len(set(r.task_id for r in results)) == 1, "not all results are for same task"
        self.filename = self.id[0]
        self.properties = self.id[1].split() if self.id[1] else []

    def set_relative_path(self, common_prefix, base_dir):
        """
        generate output representation of rows
        """
        self.short_filename = self.filename.replace(common_prefix, '', 1)


def rows_to_columns(rows):
    """
    Convert a list of Rows into a column-wise list of list of RunResult
    """
    return zip(*[row.results for row in rows])


def get_rows(runSetResults):
    """
    Create list of rows with all data. Each row consists of several RunResults.
    """
    rows = []
    for task_results in zip(*[runset.results for runset in runSetResults]):
        rows.append(Row(task_results))

    return rows


def filter_rows_with_differences(rows):
    """
    Find all rows with differences in the status column.
    """
    if not rows:
        # empty table
        return []
    if len(rows[0].results) == 1:
        # table with single column
        return []


    def get_index_of_column(name, cols):
        for i in range(0, len(cols)):
            if cols[i].title == name:
                return i
        return -1


    def all_equal_result(listOfResults):
        relevant_columns = set()
        for res in listOfResults:
            for relevant_column in res.columns_relevant_for_diff:
                relevant_columns.add(relevant_column)
        if len(relevant_columns) == 0:
            relevant_columns.add("status")

        status = []
        for col in relevant_columns:
            # It's necessary to search for the index of a column every time
            # because they can differ between results
            status.append(
                set(
                    res.values[get_index_of_column(col, res.columns)]
                    for res in listOfResults))

        return reduce(lambda x, y: x and (len(y) <= 1), status, True)


    rowsDiff = [row for row in rows if not all_equal_result(row.results)]

    if len(rowsDiff) == 0:
        logging.info("---> NO DIFFERENCE FOUND IN SELECTED COLUMNS")
    elif len(rowsDiff) == len(rows):
        logging.info("---> DIFFERENCES FOUND IN ALL ROWS, NO NEED TO CREATE DIFFERENCE TABLE")
        return []

    return rowsDiff


def get_table_head(runSetResults, commonFileNamePrefix):

    # This list contains the number of columns each run set has
    # (the width of a run set in the final table)
    # It is used for calculating the column spans of the header cells.
    runSetWidths = [len(runSetResult.columns) for runSetResult in runSetResults]

    for runSetResult in runSetResults:
        # Ugly because this overwrites the entries in the map,
        # but we don't need them anymore and this is the easiest way
        for key in runSetResult.attributes:
            values = runSetResult.attributes[key]
            if key == 'turbo':
                turbo_values = list(set(values))
                if len(turbo_values) > 1:
                    turbo = 'mixed'
                elif turbo_values[0] == 'true':
                    turbo = 'enabled'
                elif turbo_values[0] == 'false':
                    turbo = 'disabled'
                else:
                    turbo = None
                runSetResult.attributes['turbo'] = ', Turbo Boost: {}'.format(turbo) if turbo else ''

            elif key == 'timelimit':
                def fix_unit_display(value):
                    if len(value) >= 2 and value[-1] == 's' and value[-2] != ' ':
                        return value[:-1] + ' s'
                    return value
                runSetResult.attributes[key] = Util.prettylist(map(fix_unit_display, values))

            elif key == 'memlimit' or key == 'ram':
                def round_to_MB(value):
                    try:
                        return "{:.0f} MB".format(int(value)/_BYTE_FACTOR/_BYTE_FACTOR)
                    except ValueError:
                        return value
                runSetResult.attributes[key] = Util.prettylist(map(round_to_MB, values))

            elif key == 'freq':
                def round_to_MHz(value):
                    try:
                        return "{:.0f} MHz".format(int(value)/1000/1000)
                    except ValueError:
                        return value
                runSetResult.attributes[key] = Util.prettylist(map(round_to_MHz, values))

            elif key == 'host':
                runSetResult.attributes[key] = Util.prettylist(Util.merge_entries_with_common_prefixes(values))

            else:
                runSetResult.attributes[key] = Util.prettylist(values)

    def get_row(rowName, format_string, collapse=False, onlyIf=None, default='Unknown'):
        def format_cell(attributes):
            if onlyIf and not onlyIf in attributes:
                formatStr = default
            else:
                formatStr = format_string
            return formatStr.format(**attributes)

        values = [format_cell(runSetResult.attributes) for runSetResult in runSetResults]
        if not any(values):
            return None # skip row without values completely

        valuesAndWidths = list(Util.collapse_equal_values(values, runSetWidths)) \
                          if collapse else list(zip(values, runSetWidths))

        return tempita.bunch(id=rowName.lower().split(' ')[0],
                             name=rowName,
                             content=valuesAndWidths)

    titles      = [column.format_title() for runSetResult in runSetResults for column in runSetResult.columns]
    runSetWidths1 = [1]*sum(runSetWidths)
    titleRow    = tempita.bunch(id='columnTitles', name=commonFileNamePrefix,
                                content=list(zip(titles, runSetWidths1)))

    return {'tool':    get_row('Tool', '{tool} {version}', collapse=True),
            'limit':   get_row('Limits', 'timelimit: {timelimit}, memlimit: {memlimit}, CPU core limit: {cpuCores}', collapse=True),
            'host':    get_row('Host', '{host}', collapse=True, onlyIf='host'),
            'os':      get_row('OS', '{os}', collapse=True, onlyIf='os'),
            'system':  get_row('System', 'CPU: {cpu}, cores: {cores}, frequency: {freq}{turbo}; RAM: {ram}', collapse=True, onlyIf='cpu'),
            'date':    get_row('Date of execution', '{date}', collapse=True),
            'runset':  get_row('Run set', '{niceName}'),
            'branch':  get_row('Branch', '{branch}'),
            'options': get_row('Options', '{options}'),
            'property':get_row('Propertyfile', '{propertyfiles}', collapse=True, onlyIf='propertyfiles', default=''),
            'title':   titleRow}


def select_relevant_id_columns(rows):
    """
    Find out which of the entries in Row.id are equal for all given rows.
    @return: A list of True/False values according to whether the i-th part of the id is always equal.
    """
    relevant_id_columns = [True] # first column (file name) is always relevant
    if rows:
        prototype_id = rows[0].id
        for column in range(1, len(prototype_id)):
            def id_equal_to_prototype(row):
                return row.id[column] == prototype_id[column]

            relevant_id_columns.append(not all(map(id_equal_to_prototype, rows)))
    return relevant_id_columns


def get_stats_of_rows(rows):
    """Calculcate number of true/false tasks and maximum achievable score."""
    count_true = count_false = max_score = 0
    for row in rows:
        if not row.properties:
            # properties missing for at least one task, result would be wrong
            count_true = count_false = 0
            logging.info('Missing property for %s.', row.filename)
            break
        correct_result = result.satisfies_file_property(row.filename, row.properties)
        if correct_result is True:
            count_true += 1
        elif correct_result is False:
            count_false += 1
        max_score += result.score_for_task(row.filename, row.properties, result.CATEGORY_CORRECT, None)

    return max_score, count_true, count_false


def _contains_unconfirmed_results(rows_for_stats):
    for unconfirmed_stat in rows_for_stats[4]:  # 5th position in rows_for_stats is 'unconfirmed_results'
        if unconfirmed_stat and unconfirmed_stat.sum > 0:
            return True
    return False


def get_stats(rows, local_summary, correct_only):
    result_cols = list(rows_to_columns(rows))  # column-wise
    stats = list(parallel.map(get_stats_of_run_set, result_cols, [correct_only] * len(result_cols)))
    rowsForStats = list(map(Util.flatten, zip(*stats)))  # row-wise

    # find out column types for statistics columns
    if local_summary:
        rowsForStats.append(local_summary)
    columns = Util.flatten(run_result.columns for run_result in rows[0].results)
    stats_columns = []
    for i, column in enumerate(columns):
        column_values = [row[i] for row in rowsForStats]
        column_type, column_unit, column_source_unit, column_scale_factor = get_column_type(column, column_values)
        new_column = Column(column.title, column.pattern, column.number_of_significant_digits, column.href, column_type,
                            column_unit, column_source_unit, column_scale_factor, column.display_title)
        stats_columns.append(new_column)

    max_score, count_true, count_false = get_stats_of_rows(rows)
    task_counts = 'in total {0} true tasks, {1} false tasks'.format(count_true, count_false)

    if max_score:
        score_row = tempita.bunch(id='score',
                                  title='score ({0} tasks, max score: {1})'.format(len(rows), max_score),
                                  description=task_counts,
                                  content=rowsForStats[10])

    if local_summary:
        summary_row = tempita.bunch(id=None, title='local summary',
            description='(This line contains some statistics from local execution. Only trust those values, if you use your own computer.)',
            content=local_summary)

    def indent(n):
        return '&nbsp;'*(n*4)

    stats_info_correct = [
            tempita.bunch(id=None, title=indent(1)+'correct results', description='(property holds + result is true) OR (property does not hold + result is false)', content=rowsForStats[1]),
            tempita.bunch(id=None, title=indent(2)+'correct true', description='property holds + result is true', content=rowsForStats[2]),
            tempita.bunch(id=None, title=indent(2)+'correct false', description='property does not hold + result is false', content=rowsForStats[3]),
            ]
    stats_info_correct_unconfirmed = [
            tempita.bunch(id=None, title=indent(1)+'correct-unconfimed results', description='(property holds + result is true) OR (property does not hold + result is false), but unconfirmed', content=rowsForStats[4]),
            tempita.bunch(id=None, title=indent(2)+'correct-unconfirmed true', description='property holds + result is true, but unconfirmed', content=rowsForStats[5]),
            tempita.bunch(id=None, title=indent(2)+'correct-unconfirmed false', description='property does not hold + result is false, but unconfirmed', content=rowsForStats[6]),
            ]
    stats_info_wrong = [
            tempita.bunch(id=None, title=indent(1)+'incorrect results', description='(property holds + result is false) OR (property does not hold + result is true)', content=rowsForStats[7]),
            tempita.bunch(id=None, title=indent(2)+'incorrect true', description='property does not hold + result is true', content=rowsForStats[8]),
            tempita.bunch(id=None, title=indent(2)+'incorrect false', description='property holds + result is false', content=rowsForStats[9]),
            ]
    if _contains_unconfirmed_results(rowsForStats):
        stats_info = stats_info_correct + stats_info_correct_unconfirmed + stats_info_wrong
    else:
        stats_info = stats_info_correct + stats_info_wrong
    return [tempita.bunch(id=None, title='total', description=task_counts, content=rowsForStats[0]),
            ] + ([summary_row] if local_summary else []) + stats_info + ([score_row] if max_score else []), stats_columns


def get_stats_of_run_set(runResults, correct_only):
    """
    This function returns the numbers of the statistics.
    @param runResults: All the results of the execution of one run set (as list of RunResult objects)
    """

    # convert:
    # [['TRUE', 0,1], ['FALSE', 0,2]] -->  [['TRUE', 'FALSE'], [0,1, 0,2]]
    # in python2 this is a list, in python3 this is the iterator of the list
    # this works, because we iterate over the list some lines below
    listsOfValues = zip(*[runResult.values for runResult in runResults])

    columns = runResults[0].columns
    main_status_list = [(runResult.category, runResult.status) for runResult in runResults]

    # collect some statistics
    totalRow = []
    correctRow = []
    correctTrueRow = []
    correctFalseRow = []
    correctUnconfirmedRow = []
    correctUnconfirmedTrueRow = []
    correctUnconfirmedFalseRow = []
    incorrectRow = []
    wrongTrueRow = []
    wrongFalseRow = []
    scoreRow = []

    status_col_index = 0  # index of 'status' column
    for index, (column, values) in enumerate(zip(columns, listsOfValues)):
        col_type = column.type.type
        if col_type != ColumnType.text:
            if col_type == ColumnType.main_status or col_type == ColumnType.status:
                if col_type == ColumnType.main_status:
                    status_col_index = index
                    score = StatValue(sum(run_result.score for run_result in runResults))
                else:
                    score = None

                total   = StatValue(len([runResult.values[index] for runResult in runResults if runResult.status]))

                curr_status_list = [(runResult.category, runResult.values[index]) for runResult in runResults]

                counts = collections.Counter((category, result.get_result_classification(status))
                                             for category, status in curr_status_list)
                countCorrectTrue             = counts[result.CATEGORY_CORRECT,             result.RESULT_CLASS_TRUE]
                countCorrectFalse            = counts[result.CATEGORY_CORRECT,             result.RESULT_CLASS_FALSE]
                countCorrectUnconfirmedTrue  = counts[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_TRUE]
                countCorrectUnconfirmedFalse = counts[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_FALSE]
                countWrongTrue               = counts[result.CATEGORY_WRONG,               result.RESULT_CLASS_TRUE]
                countWrongFalse              = counts[result.CATEGORY_WRONG,               result.RESULT_CLASS_FALSE]

                correct                 = StatValue(countCorrectTrue + countCorrectFalse)
                correctTrue             = StatValue(countCorrectTrue)
                correctFalse            = StatValue(countCorrectFalse)
                correctUnconfirmed      = StatValue(countCorrectUnconfirmedTrue + countCorrectUnconfirmedFalse)
                correctUnconfirmedTrue  = StatValue(countCorrectUnconfirmedTrue)
                correctUnconfirmedFalse = StatValue(countCorrectUnconfirmedFalse)
                incorrect               = StatValue(countWrongTrue + countWrongFalse)
                wrongTrue               = StatValue(countWrongTrue)
                wrongFalse              = StatValue(countWrongFalse)

            else:
                assert column.is_numeric()
                total, correct, correctTrue, correctFalse,\
                       correctUnconfirmed, correctUnconfirmedTrue, correctUnconfirmedFalse,\
                       incorrect, wrongTrue, wrongFalse =\
                    get_stats_of_number_column(values, main_status_list, column.title, correct_only)

                score = None

        else:
            total, correct, correctTrue, correctFalse,\
                   correctUnconfirmed, correctUnconfirmedTrue, correctUnconfirmedFalse,\
                   incorrect, wrongTrue, wrongFalse =\
                (None, None, None, None, None, None, None, None, None, None)
            score = None

        totalRow.append(total)
        correctRow.append(correct)
        correctTrueRow.append(correctTrue)
        correctFalseRow.append(correctFalse)
        correctUnconfirmedRow.append(correctUnconfirmed)
        correctUnconfirmedTrueRow.append(correctUnconfirmedTrue)
        correctUnconfirmedFalseRow.append(correctUnconfirmedFalse)
        incorrectRow.append(incorrect)
        wrongTrueRow.append(wrongTrue)
        wrongFalseRow.append(wrongFalse)
        scoreRow.append(score)

    def replace_irrelevant(row):
        if not row:
            return
        count = row[status_col_index]
        if not count or not count.sum:
            for i in range(1, len(row)):
                row[i] = None

    replace_irrelevant(totalRow)
    replace_irrelevant(correctRow)
    replace_irrelevant(correctTrueRow)
    replace_irrelevant(correctFalseRow)
    replace_irrelevant(correctUnconfirmedRow)
    replace_irrelevant(correctUnconfirmedTrueRow)
    replace_irrelevant(correctUnconfirmedFalseRow)
    replace_irrelevant(incorrectRow)
    replace_irrelevant(wrongTrueRow)
    replace_irrelevant(wrongFalseRow)
    replace_irrelevant(scoreRow)

    stats = (totalRow, correctRow, correctTrueRow, correctFalseRow,\
                       correctUnconfirmedRow, correctUnconfirmedTrueRow, correctUnconfirmedFalseRow,\
                       incorrectRow, wrongTrueRow, wrongFalseRow,\
             scoreRow)
    return stats


class StatValue(object):
    def __init__(self, sum, min=None, max=None, avg=None, median=None, stdev=None):  # @ReservedAssignment
        self.sum = sum
        self.min = min
        self.max = max
        self.avg = avg
        self.median = median
        self.stdev = stdev

    def __str__(self):
        return str(self.sum)

    @classmethod
    def from_list(cls, values):
        if any(math.isnan(v) for v in values if v is not None):
            return StatValue(nan, nan, nan, nan, nan, nan)

        values = sorted(v for v in values if v is not None)
        if not values:
            return StatValue(0)

        values_len = len(values)
        min_value = values[0]
        max_value = values[-1]

        if min_value == -inf and max_value == +inf:
            values_sum = nan
            mean = nan
            stdev = nan
        elif max_value == inf:
            values_sum = inf
            mean = inf
            stdev = inf
        elif min_value == -inf:
            values_sum = -inf
            mean = -inf
            stdev = inf
        else:
            values_sum = sum(values)
            mean = values_sum / values_len

            stdev = Decimal(0)
            for v in values:
                diff = v - mean
                stdev += diff * diff
            stdev = (stdev / values_len).sqrt()

        half, len_is_odd = divmod(values_len, 2)
        if len_is_odd:
            median = values[half]
        else:
            median = (values[half-1] + values[half]) / Decimal(2)

        return StatValue(values_sum,
                         min    = min_value,
                         max    = max_value,
                         avg    = mean,
                         median = median,
                         stdev = stdev,
                         )


def get_stats_of_number_column(values, categoryList, columnTitle, correct_only):
    assert len(values) == len(categoryList)
    try:
        valueList = [Util.to_decimal(v) for v in values]
    except InvalidOperation as e:
        if columnTitle != "host" and not columnTitle.endswith('status'): # We ignore values of columns 'host' and 'status'.
            logging.warning("%s. Statistics may be wrong.", e)
        return (StatValue(0), StatValue(0), StatValue(0), StatValue(0),\
                              StatValue(0), StatValue(0), StatValue(0),\
                              StatValue(0), StatValue(0), StatValue(0))

    valuesPerCategory = collections.defaultdict(list)
    for value, catStat in zip(valueList, categoryList):
        category, status = catStat
        if status is None:
            continue
        valuesPerCategory[category, result.get_result_classification(status)].append(value)

    return (StatValue.from_list(valueList),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT, result.RESULT_CLASS_TRUE]
                              + valuesPerCategory[result.CATEGORY_CORRECT, result.RESULT_CLASS_FALSE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT, result.RESULT_CLASS_TRUE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT, result.RESULT_CLASS_FALSE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_TRUE]
                              + valuesPerCategory[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_FALSE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_TRUE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_CORRECT_UNCONFIRMED, result.RESULT_CLASS_FALSE]),
            StatValue.from_list(valuesPerCategory[result.CATEGORY_WRONG, result.RESULT_CLASS_TRUE]
                              + valuesPerCategory[result.CATEGORY_WRONG, result.RESULT_CLASS_FALSE]) if not correct_only else None,
            StatValue.from_list(valuesPerCategory[result.CATEGORY_WRONG, result.RESULT_CLASS_TRUE]) if not correct_only else None,
            StatValue.from_list(valuesPerCategory[result.CATEGORY_WRONG, result.RESULT_CLASS_FALSE]) if not correct_only else None,
            )


def get_regression_count(rows, ignoreFlappingTimeouts): # for options.dump_counts
    """Count the number of regressions, i.e., differences in status of the two right-most results
    where the new one is not "better" than the old one.
    Any change in status between error, unknown, and wrong result is a regression.
    Different kind of errors or wrong results are also a regression.
    """

    def status_is(run_result, status):
        # startswith is used because status can be "TIMEOUT (TRUE)" etc., which count as "TIMEOUT"
        return run_result.status and run_result.status.startswith(status)

    def any_status_is(run_results, status):
        for run_result in run_results:
            if status_is(run_result, status):
                return True
        return False

    regressions = 0
    for row in rows:
        if len(row.results) < 2:
            return 0 # no regressions at all with only one run

        # "new" and "old" are the latest two results
        new = row.results[-1]
        old = row.results[-2]

        if new.category == result.CATEGORY_CORRECT:
            continue # no regression if result is correct

        if new.status == old.status:
            continue # no regression if result is the same as before
        if status_is(new, 'TIMEOUT') and status_is(old, 'TIMEOUT'):
            continue # no regression if both are some form of timeout
        if status_is(new, 'OUT OF MEMORY') and status_is(old, 'OUT OF MEMORY'):
            continue # same for OOM

        if (ignoreFlappingTimeouts and
                status_is(new, 'TIMEOUT') and any_status_is(row.results[:-2], 'TIMEOUT')):
            continue # flapping timeout because any of the older results is also a timeout

        regressions += 1

    return regressions


def get_counts(rows): # for options.dump_counts
    countsList = []

    for runResults in rows_to_columns(rows):
        counts = collections.Counter(runResult.category for runResult in runResults)
        countsList.append((counts[result.CATEGORY_CORRECT],
                           counts[result.CATEGORY_WRONG],
                           counts[result.CATEGORY_UNKNOWN]
                           + counts[result.CATEGORY_ERROR]
                           + counts[None], # for rows without a result
                          ))

    return countsList


def get_summary(runSetResults):
    summaryStats = []
    available = False
    for runSetResult in runSetResults:
        for column in runSetResult.columns:
            if column.is_numeric() \
                    and column.title in runSetResult.summary and runSetResult.summary[column.title] != '':

                available = True
                try:
                    value = StatValue(Util.to_decimal(runSetResult.summary[column.title]))
                except InvalidOperation:
                    value = None
            else:
                value = None
            summaryStats.append(value)

    return summaryStats if available else None


def create_tables(name, runSetResults, rows, rowsDiff, outputPath, outputFilePattern, options):
    '''
    Create tables and write them to files.
    @return a list of futures to allow waiting for completion
    '''

    # get common folder of sourcefiles
    common_prefix = os.path.commonprefix([r.filename for r in rows]) # maybe with parts of filename
    common_prefix = common_prefix[: common_prefix.rfind('/') + 1] # only foldername
    for row in rows:
        Row.set_relative_path(row, common_prefix, outputPath)

    # compute nice name of each run set for displaying
    firstBenchmarkName = Util.prettylist(runSetResults[0].attributes['benchmarkname'])
    allBenchmarkNamesEqual = all(Util.prettylist(r.attributes['benchmarkname']) == firstBenchmarkName for r in runSetResults)
    for r in runSetResults:
        if not r.attributes['name']:
            r.attributes['niceName'] = r.attributes['benchmarkname']
        elif allBenchmarkNamesEqual:
            r.attributes['niceName'] = r.attributes['name']
        else:
            r.attributes['niceName'] = [Util.prettylist(r.attributes['benchmarkname']) + '.' + Util.prettylist(r.attributes['name'])]

    template_values = lambda: None # dummy object as dict replacement for simpler syntax
    template_values.head = get_table_head(runSetResults, common_prefix)
    template_values.run_sets = [runSetResult.attributes for runSetResult in runSetResults]
    template_values.columns = [[column for column in runSet.columns] for runSet in runSetResults]
    template_values.columnTitles =\
        [[column.format_title() for column in runSet.columns] for runSet in runSetResults]

    template_values.relevant_id_columns = select_relevant_id_columns(rows)
    template_values.count_id_columns = template_values.relevant_id_columns.count(True)

    template_values.lib_url = options.lib_url
    template_values.base_dir = outputPath
    template_values.href_base = os.path.dirname(options.xmltablefile) if options.xmltablefile else None
    template_values.version = __version__

    futures = []

    def write_table(table_type, title, rows, use_local_summary):
        # calculate statistics if necessary
        if not options.format == ['csv']:
            local_summary = get_summary(runSetResults) if use_local_summary else None
            stats, stats_columns = get_stats(rows, local_summary, options.correct_only)
        else:
            stats = stats_columns = None

        for template_format in (options.format or TEMPLATE_FORMATS):
            if outputFilePattern == '-':
                outfile = None
                logging.info('Writing %s to stdout...', template_format.upper().ljust(4))
            else:
                outfile = os.path.join(outputPath, outputFilePattern.format(name=name, type=table_type, ext=template_format))
                logging.info('Writing %s into %s ...', template_format.upper().ljust(4), outfile)

            this_template_values = dict(title=title, body=rows, foot=stats, foot_columns=stats_columns)
            this_template_values.update(template_values.__dict__)

            futures.append(parallel.submit(
                write_table_in_format,
                template_format, outfile, this_template_values,
                options.show_table and template_format == 'html',
                ))

    # write normal tables
    write_table("table", name, rows,
                use_local_summary=(not options.correct_only and not options.common))

    # write difference tables
    if rowsDiff:
        write_table("diff", name + " differences", rowsDiff, use_local_summary=False)

    return futures


def write_table_in_format(template_format, outfile, template_values, show_table):
    # read template
    Template = tempita.HTMLTemplate if template_format == 'html' else tempita.Template
    template_file = TEMPLATE_FILE_NAME.format(format=template_format)
    try:
        template_content = __loader__.get_data(template_file).decode(TEMPLATE_ENCODING)
    except NameError:
        with open(template_file, mode='r') as f:
            template_content = f.read()
    template = Template(template_content, namespace=TEMPLATE_NAMESPACE)

    result = template.substitute(**template_values)

    # write file
    if not outfile:
        print(result, end='')
    else:
        with open(outfile, 'w') as file:
            file.write(result)

        if show_table:
            try:
                with open(os.devnull, 'w') as devnull:
                    subprocess.Popen(['xdg-open', outfile],
                                     stdout=devnull, stderr=devnull)
            except OSError:
                pass


def basename_without_ending(file):
    name = os.path.basename(file)
    if name.endswith(".xml"):
        name = name[:-4]
    elif name.endswith(".xml.gz"):
        name = name[:-7]
    elif name.endswith(".xml.bz2"):
        name = name[:-8]
    return name


def create_argument_parser():
    parser = argparse.ArgumentParser(
        fromfile_prefix_chars='@',
        description=
        """Create tables with the results of one or more benchmark executions.
           Command-line parameters can additionally be read from a file if file name prefixed with '@' is given as argument.
           Part of BenchExec: https://github.com/sosy-lab/benchexec/"""
    )

    parser.add_argument("tables",
        metavar="RESULT",
        type=str,
        nargs='*',
        help="XML file with the results from the benchmark script"
    )
    parser.add_argument("-x", "--xml",
        action="store",
        type=str,
        dest="xmltablefile",
        help="XML file with the table definition."
    )
    parser.add_argument("-o", "--outputpath",
        action="store",
        type=str,
        dest="outputPath",
        help="Output path for the tables. If '-', the tables are written to stdout."
    )
    parser.add_argument("-n", "--name",
        action="store",
        type=str,
        dest="output_name",
        help="Base name of the created output files."
    )
    parser.add_argument("--ignore-erroneous-benchmarks",
        action="store_true",
        dest="ignore_errors",
        help="Ignore incomplete result files or results where the was an error during benchmarking."
    )
    parser.add_argument("-d", "--dump",
        action="store_true", dest="dump_counts",
        help="Print summary statistics for regressions and the good, bad, and unknown counts."
    )
    parser.add_argument("--ignore-flapping-timeout-regressions",
        action="store_true", dest="ignoreFlappingTimeouts",
        help="For the regression-count statistics, do not count regressions to timeouts if the file already had timeouts before."
    )
    parser.add_argument("-f", "--format",
        action="append",
        choices=TEMPLATE_FORMATS,
        help="Which format to generate (HTML or CSV). Can be specified multiple times. If not specified, all are generated."
    )
    parser.add_argument("-c", "--common",
        action="store_true", dest="common",
        help="Put only sourcefiles into the table for which all benchmarks contain results."
    )
    parser.add_argument("--no-diff",
        action="store_false", dest="write_diff_table",
        help="Do not output a table with result differences between benchmarks."
    )
    parser.add_argument("--correct-only",
        action="store_true", dest="correct_only",
        help="Clear all results (e.g., time) in cases where the result was not correct."
    )
    parser.add_argument("--all-columns",
        action="store_true", dest="all_columns",
        help="Show all columns in tables, including those that are normally hidden."
    )
    parser.add_argument("--offline",
        action="store_const", dest="lib_url",
        const=LIB_URL_OFFLINE,
        default=LIB_URL,
        help="Expect JS libs in libs/javascript/ instead of retrieving them from a CDN. "
            "Currently does not work for all libs."
    )
    parser.add_argument("--show",
        action="store_true", dest="show_table",
        help="Open the produced HTML table(s) in the default browser."
    )
    parser.add_argument("-q", "--quiet",
        action="store_true",
        help="Do not show informational messages, only warnings."
    )
    parser.add_argument("--version",
        action="version", version="%(prog)s " + __version__
    )
    return parser


def sigint_handler(*args, **kwargs):
    # Use SystemExit instead of KeyboardInterrupt to avoid ugly stack traces for each worker
    sys.exit(1)


def main(args=None):
    if sys.version_info < (3,):
        sys.exit('table-generator needs Python 3 to run.')
    signal.signal(signal.SIGINT, sigint_handler)

    arg_parser = create_argument_parser()
    options = arg_parser.parse_args((args or sys.argv)[1:])

    logging.basicConfig(format="%(levelname)s: %(message)s",
                        level=logging.WARNING if options.quiet else logging.INFO)

    global parallel
    import concurrent.futures
    cpu_count = 1
    try:
        cpu_count = os.cpu_count() or 1
    except AttributeError:
        pass
    # Use up to cpu_count*2 workers because some tasks are I/O bound.
    parallel = concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count*2)

    name = options.output_name
    outputPath = options.outputPath
    if outputPath == '-':
        # write to stdout
        outputFilePattern = '-'
        outputPath = '.'
    else:
        outputFilePattern = "{name}.{type}.{ext}"

    if options.xmltablefile:
        try:
            table_definition = parse_table_definition_file(options.xmltablefile)

            if table_definition_lists_result_files(table_definition):
                if options.tables:
                    arg_parser.error(
                        "Invalid additional arguments '{}'.".format(" ".join(options.tables)))

                runSetResults = load_results_from_table_definition(
                    table_definition, options.xmltablefile, options)

            else:
                if not options.tables:
                    arg_parser.error(
                        "No result files given. Either list them on the command line "
                        "or with <result> tags in the table-definiton file.")

                result_files = Util.extend_file_list(options.tables) # expand wildcards
                runSetResults = load_results_with_table_definition(
                    result_files, table_definition, options.xmltablefile, options)

        except Util.TableDefinitionError as e:
            logging.error('Fault in {}: {}'.format(options.xmltablefile, e.message))
            exit(1)

        if not name:
            name = basename_without_ending(options.xmltablefile)

        if not outputPath:
            outputPath = os.path.dirname(options.xmltablefile)

    else:
        if options.tables:
            inputFiles = options.tables
        else:
            searchDir = outputPath or DEFAULT_OUTPUT_PATH
            logging.info("Searching result files in '%s'...", searchDir)
            inputFiles = [os.path.join(searchDir, '*.results*.xml')]

        inputFiles = Util.extend_file_list(inputFiles) # expand wildcards
        runSetResults = load_results(inputFiles, options)

        if len(inputFiles) == 1:
            if not name:
                name = basename_without_ending(inputFiles[0])
            if not outputFilePattern == '-':
                outputFilePattern = "{name}.{ext}"
        else:
            if not name:
                name = NAME_START + "." + time.strftime("%Y-%m-%d_%H%M", time.localtime())

        if inputFiles and not outputPath:
            path = os.path.dirname(inputFiles[0])
            if not "://" in path and all(path == os.path.dirname(file) for file in inputFiles):
                outputPath = path
            else:
                outputPath = DEFAULT_OUTPUT_PATH

    if not outputPath:
        outputPath = '.'

    runSetResults = [r for r in runSetResults if r is not None]
    if not runSetResults:
        logging.error('No benchmark results found.')
        exit(1)

    logging.info('Merging results...')
    if options.common:
        find_common_tasks(runSetResults)
    else:
        # merge list of run sets, so that all run sets contain the same tasks
        merge_tasks(runSetResults)

    rows     = get_rows(runSetResults)
    if not rows:
        logging.warning('No results found, no tables produced.')
        exit()
    rowsDiff = filter_rows_with_differences(rows) if options.write_diff_table else []

    logging.info('Generating table...')
    if not os.path.isdir(outputPath) and not outputFilePattern == '-':
        os.makedirs(outputPath)
    futures = create_tables(name, runSetResults, rows, rowsDiff, outputPath, outputFilePattern, options)

    if options.dump_counts: # print some stats for Buildbot
        print ("REGRESSIONS {}".format(get_regression_count(rows, options.ignoreFlappingTimeouts)))
        countsList = get_counts(rows)
        print ("STATS")
        for counts in countsList:
            print (" ".join(str(e) for e in counts))

    for f in futures:
        f.result() # to get any exceptions that may have occurred
    logging.info('done')

    parallel.shutdown(wait=True)


if __name__ == '__main__':
    sys.exit(main())
