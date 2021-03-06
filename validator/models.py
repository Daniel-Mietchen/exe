#!/usr/bin/env python2
# -*- coding: utf-8 -*-
##############################################################################
#
#   sci.AI EXE
#   Copyright(C) 2017 sci.AI
#
#   This program is free software: you can redistribute it and / or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of the
#   License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY
#   without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with this program.  If not, see < http://www.gnu.org/licenses/ >.
#
##############################################################################

"""
    App models
"""

import urllib
import re
import io
from datetime import datetime

from bs4 import BeautifulSoup
from mongoengine import *
from flask import render_template

import nbformat
from nbconvert.preprocessors import ExecutePreprocessor
from nbconvert.preprocessors.execute import CellExecutionError
from nbconvert import HTMLExporter

from validator import db, queue
from validator.utils import get_path_to_file, install_dependencies, \
    generate_id, is_allowed_file, read_csv_file, get_uploads_path, \
    get_direct_url_to_notebook, render_without_request

from rq import get_current_job


class Task(db.Document):
    """
        Entity represents tasks
    """
    task_id = db.StringField()
    list_id = db.StringField()
    date_created = db.DateTimeField(default=datetime.now())

    # meta info
    meta = {
        'collection': 'tasks',
        'db_alias': 'main'
    }

    def get_id(self):
        """
            Returns entity ID
        """
        return str(self.task_id)

    @staticmethod
    def create_task(list_type, content):
        """
            Create a new task
        """
        queue_task = queue.enqueue(
            List.create_list,
            list_type,
            content
        )

        new_task = Task(
            task_id=queue_task.id
        )

        new_task.save()

        Log.write_log(
            new_task.get_id(),
            None,
            None,
            'List of references added to the processing queue: {0} position'.format(queue.count)
        )

        return new_task.get_id()


class List(db.Document):
    """
        Entity represents list of urls to papers record
    """
    task_id = db.StringField()
    filename = db.StringField(default='')
    extension = db.StringField(default='csv')
    path = db.StringField(default='')
    date_created = db.DateTimeField(default=datetime.now())
    date_updated = db.DateTimeField(default=datetime.now())
    is_processed = db.BooleanField(default=False)
    list_type = db.StringField(db_field="type")
    # meta info
    meta = {
        'collection': 'lists',
        'db_alias': 'main'
    }

    def get_id(self):
        """
            Returns entity ID
        """
        return str(self.id)


    @classmethod
    def create_list(cls, list_type, content):
        """
            Returns new list
        """
        # get task ID
        task = get_current_job()

        new_list = List(list_type=list_type, task_id=task.id)
        new_list.save()

        Log.write_log(
            new_list.get_id(),
            None,
            None,
            'Successfully saved file with list of links'
        )

        if new_list.list_type == 'file':
            saving_file_status = new_list.update_file(content)
            if saving_file_status:
                urls_to_papers = new_list.extract_list_of_links()
            else:
                Log.write_log(
                    new_list.get_id(),
                    None,
                    None,
                    'Wrong file format'
                )
        else:
            urls_to_papers = content

        # process links to papers
        for url_to_paper in urls_to_papers:
            new_paper = Paper.create_new_paper(new_list.get_id(), url_to_paper)
            # get NB urls for paper
            notebooks_urls = new_paper.extract_links_to_notebooks()
            for notebook_url in notebooks_urls:
                Notebook.create_new_notebook(
                    new_list.get_id(),
                    new_paper.get_id(),
                    notebook_url
                )
            new_paper.mark_as_done()
        new_list.is_processed = True
        new_list.date_updated = datetime.now()
        new_list.save()
        return new_list


    def update_type(self, list_type):
        """
            Update type of list
        """
        self.list_type = list_type
        self.save()

        Log.write_log(
            self.get_id(),
            None,
            None,
            'Successfully updated type of list of links to {0}'.format(list_type)
        )


    def update_file(self, filename):
        """
            Create a new file with list of links
        """
        self.filename = filename
        self.path = get_path_to_file(filename)
        self.save()
        Log.write_log(
            self.get_id(),
            None,
            None,
            'Successfully updated file with list of links'
        )
        return True

    def extract_list_of_links(self):
        """
            Extract list of links to notebooks from papers
        """
        Log.write_log(
            self.get_id(),
            None,
            None,
            'Starting processing list of urls'
        )

        urls_to_papers = read_csv_file(get_path_to_file(self.filename))

        Log.write_log(
            self.get_id(),
            None,
            None,
            'Total papers urls extracted: {0}'.format(len(urls_to_papers))
        )

        return urls_to_papers


class Paper(db.Document):
    """
        Entity represents paper record
    """
    original_url = db.StringField()
    download_url = db.StringField(default='')
    url_type = db.StringField(default='paper')
    list_id = db.StringField()
    date_created = db.DateTimeField(default=datetime.now())
    is_processed = db.BooleanField(default=False)
    
    # meta info
    meta = {
        'collection': 'papers',
        'db_alias': 'main'
    }

    def get_id(self):
        """
            Returns entity ID
        """
        return str(self.id)

    @staticmethod
    def create_new_paper(list_id, url_to_paper):
        """
            Returns new paper object
        """
        Log.write_log(
            list_id,
            None,
            None,
            'Process paper url: {0}'.format(url_to_paper)
        )
        
        new_paper = Paper(
            original_url=url_to_paper,
            list_id=list_id,
            url_type=Paper.get_type_of_url(url_to_paper)
        )
        new_paper.save()
        return new_paper


    @staticmethod
    def get_type_of_url(url):
        """
            Return type of URL: DOI or direct
        """
        # https://stackoverflow.com/questions/27910/finding-a-doi-in-a-document-or-page
        m = re.match(r'\b(10[.][0-9]{4,}(?:[.][0-9]+)*/(?:(?![\"&\'<>])\S)+)\b', url)
        if m:
            return 'doi'
        return 'direct'


    def mark_as_done(self):
        """
            Update status of paper
        """
        self.is_processed = True
        self.save()
        Log.write_log(
            self.list_id,
            self.get_id(),
            None,
            'Paper marked as done'
        )
        return True

    def get_download_url(self):
        if self.original_url.find('ncbi.nlm.nih.gov') > -1:
            res = re.findall(r'articles\/(.*)\/?', self.original_url)
            if res:
                pub_id = res[0]
                self.download_url = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={0}'.format(
                    pub_id)
        elif re.match(r'\b(10[.][0-9]{4,}(?:[.][0-9]+)*/(?:(?![\"&\'<>])\S)+)\b', self.original_url):
            self.download_url = 'https://dx.doi.org/' + self.original_url
        self.save()


    def extract_links_to_notebooks(self):
        """
            Returns list of links to notebooks from paper
        """
        Log.write_log(
            self.list_id,
            self.get_id(),
            None,
            'Start downloading paper from URL: {0}'.format(self.original_url)
        )
        self.get_download_url()
        try:
            r = urllib.urlopen(self.download_url).read()
        except Exception as e:
            Log.write_log(
                self.list_id,
                self.get_id(),
                None,
                'Caught exception when try to download paper: {0}'.format(str(e))
            )
            return []
        soup = BeautifulSoup(r)

        Log.write_log(
            self.list_id,
            self.get_id(),
            None,
            'Downloaded paper from URL: {0}'.format(self.download_url)
        )

        notebooks_urls = []
        for url in soup.find_all(['a', 'ext-link']):
            current_url = url.get('href') if url.get(
                'href') else url.get('xlink:href')
            if current_url and current_url.endswith('ipynb'):
                notebooks_urls.append(current_url)

        notebooks_urls = list(set(notebooks_urls))

        Log.write_log(
            self.list_id,
            self.get_id(),
            None,
            'Found {0} links to notebooks in paper'.format(len(notebooks_urls))
        )

        return notebooks_urls



class Notebook(db.Document):
    """
        Entity represents notebook record
    """
    original_url = db.StringField()
    download_url = db.StringField(default='')
    filename = db.StringField(default='')
    path = db.StringField(default='')
    output_path = db.StringField(default='')
    output_html_path = db.StringField(default='')
    list_id = db.StringField()
    paper_id = db.StringField()
    date_created = db.DateTimeField(default=datetime.now())
    is_failed = db.BooleanField(default=False)
    is_processed = db.BooleanField(default=False)
    is_downloaded = db.BooleanField(default=False)
    message = db.StringField(default='')
    kernel = db.StringField(default='')
    # meta info
    meta = {
        'collection': 'notebooks',
        'db_alias': 'main'
    }


    def get_id(self):
        """
            Returns entity ID
        """
        return str(self.id)

    @staticmethod
    def create_new_notebook(list_id, paper_id, notebook_url):
        """
            Returns new notebook object
        """
        new_notebook = Notebook(
            original_url=notebook_url,
            list_id=list_id,
            paper_id=paper_id
        )
        new_notebook.save()

        notebook_filename = new_notebook.get_id() + '.ipynb'

        new_notebook.filename = notebook_filename
        new_notebook.path = get_path_to_file(notebook_filename)
        new_notebook.output_path = get_path_to_file(
            'v_output_{0}'.format(notebook_filename))
        new_notebook.output_html_path = get_path_to_file(
            '{0}.html'.format(notebook_filename))

        new_notebook.download_notebook()
        new_notebook.process_notebook()

        return new_notebook.get_id()


    def download_notebook(self):
        """
            Download notebook to uploads folder
        """
        Log.write_log(
            self.list_id,
            self.paper_id,
            self.get_id(),
            'Start downloading notebook: {0}'.format(self.original_url)
        )

        self.download_url = get_direct_url_to_notebook(self.original_url)

        Log.write_log(
            self.list_id,
            self.paper_id,
            self.get_id(),
            'URL to download notebook: {0}'.format(self.download_url)
        )

        urllib.urlretrieve(
            self.download_url,
            self.path
        )
        self.is_downloaded = True
        self.save()

        Log.write_log(
            self.list_id,
            self.paper_id,
            self.get_id(),
            'Downloaded notebook: {0}'.format(self.download_url)
        )


    def process_notebook(self):
        """
            Process notebook
        """
        Log.write_log(
            self.list_id,
            self.paper_id,
            self.get_id(),
            'Start processing notebook: {0}'.format(self.original_url)
        )
        try:
            notebook_file = io.open(self.path, encoding='utf-8')
            notebook_content = nbformat.read(notebook_file, as_version=4)
            # clear outputs
            notebook_content = Notebook.clear_outputs(notebook_content)
            # get kernel name
            kernel_name = notebook_content['metadata']['kernelspec']['name']
            status, installation_log = install_dependencies(
                str(notebook_content),
                kernel_name
            )

            Log.write_log(
                self.list_id,
                self.paper_id,
                self.get_id(),
                'Install dependencies log: {0}'.format(installation_log)
            )

            ep = ExecutePreprocessor(
                timeout=3600,
                kernel_name=kernel_name
            )
            ep.preprocess(
                notebook_content,
                {'metadata': {'path': get_uploads_path()}}
            )
            message = 'Successfully processed'
            is_failed = False

            Log.write_log(
                self.list_id,
                self.paper_id,
                self.get_id(),
                'Successfully processed notebook: {0}'.format(self.original_url)
            )
        except Exception as e:
            message = str(e)
            Log.write_log(
                self.list_id,
                self.paper_id,
                self.get_id(),
                'Caught exception when process: {0}'.format(message)
            )

            kernel_name = None if 'kernel_name' not in locals() else kernel_name
            is_failed = True
            notebook_content = False if 'notebook_content' not in locals() else notebook_content
        finally:
            self.kernel = kernel_name
            self.message = message
            self.is_failed = is_failed
            self.is_processed = True
            self.save()
            if notebook_content:
                Log.write_log(
                    self.list_id,
                    self.paper_id,
                    self.get_id(),
                    'Start writing HTML output to file'
                )
                html_exporter = HTMLExporter()
                html_exporter.template_file = 'full'
                (body, resources) = html_exporter.from_notebook_node(notebook_content)
                f = open(self.output_html_path, 'w')
                f.write(body.encode('utf-8'))
                f.close()
                with io.open(self.output_path, mode='wt', encoding='utf-8') as f:
                    nbformat.write(notebook_content, f)
        return self.get_id()

    @staticmethod
    def clear_outputs(notebook, clear_prompt_numbers=True):
        """
            Clears the output of all cells in an ipython notebook
        """
        for cell in notebook.cells:
            if cell.get('cell_type', None) == 'code':
                cell.outputs = []
                if clear_prompt_numbers is True:
                    cell.execution_count = None
                    cell.pop('prompt_number', None)

        return notebook


class Log(db.Document):
    """
        Entity representes log record
    """
    date_created = db.DateTimeField(default=datetime.now())
    list_id = db.StringField()
    paper_id = db.StringField()
    notebook_id = db.StringField()
    message = db.StringField()
    # meta info
    meta = {
        'collection': 'logs',
        'db_alias': 'main'
    }

    def get_id(self):
        """
            Returns entity ID
        """
        return str(self.id)


    @staticmethod
    def write_log(list_id, paper_id, notebook_id, message):
        """
            Write log
        """
        new_log = Log(
            list_id=list_id,
            paper_id=paper_id,
            notebook_id=notebook_id,
            message=message
        )
        new_log.save()
        return new_log.get_id()
