#!/usr/bin/env python3

import os
import sys
import configparser
import logging

import click

from flask import Flask, request, make_response
from jira import JIRA
from jinja2 import Template
import prometheus_client as prometheus
import base64

app = Flask(__name__)

jira = None

summary_tmpl = Template(r'{% if commonAnnotations.summary %}{{ commonAnnotations.summary }}{% else %}{% for k, v in groupLabels.items() %}{{ k }}="{{v}}" {% endfor %}{% endif %}')

description_tmpl = Template(r'''

Alerts can be "Grouped". Below is both the common and specific information associated with this alert. For more
information, see the [Alertmanager docs|https://prometheus.io/docs/alerting/configuration/#route]

h2. Labels common across this alert group

{% for k, v in commonAnnotations.items() -%}
* *{{ k }}*: {{ v }}
{% endfor %}

h2. Each alert's labels in this group

{% for a in alerts if a.status == 'firing' -%}
_Annotations_:
  {% for k, v in a.annotations.items() -%}
* {{ k }} = {{ v }}
  {% endfor %}
_Labels_:
  {% for k, v in a.labels.items() -%}
* {{ k }} = {{ v }}
  {% endfor %}
[Source|{{ a.generatorURL }}]
----
{% endfor %}


alert_group_key={{ groupKey }}
jira_reference={{ jiraReference }}
''')

description_boundary = '_-- Alertmanager -- [only edit above]_'

jira_request_time = prometheus.Histogram('jira_request_latency_seconds', 'Latency when querying the JIRA API', ['action'])
request_time = prometheus.Histogram('request_latency_seconds', 'Latency of incoming requests')

jira_request_time_transitions = jira_request_time.labels({'action': 'transitions'})
jira_request_time_close = jira_request_time.labels({'action': 'close'})
jira_request_time_reopen = jira_request_time.labels({'action': 'reopen'})
jira_request_time_update = jira_request_time.labels({'action': 'update'})
jira_request_time_create = jira_request_time.labels({'action': 'create'})
jira_request_time_query = jira_request_time.labels({'action': 'query'})

@jira_request_time_query.time()
def query(reference):
    search_query = 'description ~ "jira_reference=%s"'

    if jira_config.get('reopen', 'False') == 'False':
        search_query = search_query + ' and status != "%s"' % (jira_config.get('issue_closed', 'Closed'))

    logger.info('Attempting to query tickets with the following query: ' + search_query % (reference))

    result = jira.search_issues(search_query % (reference))

    if result:
        return result[0]

    return False

@jira_request_time_transitions.time()
def transitions(issue):
    logger.info('Requesting a list of transitions for "%s"' % issue.id)
    return jira.transitions(issue)

@jira_request_time_close.time()
def close(issue, tid):
    logger.info('Issue "%s" appears to be resolved. Closing' % issue.id)
    return jira.transition_issue(issue, tid)

@jira_request_time_reopen.time()
def reopen(issue, tid):
    logger.info('Issue has "%s" reoccurred. Reopening' % issue.id)
    return jira.transition_issue(issue, tid)

@jira_request_time_update.time()
def update_issue(issue, summary, description):
    logger.info('Issue "%s" was found. Updating' % issue.id)

    custom_desc = issue.fields.description.rsplit(description_boundary, 1)[0]
    return issue.update(
        summary=summary,
        description="%s\n\n%s\n%s" % (custom_desc.strip(), description_boundary, description))

@jira_request_time_create.time()
def create_issue(project, team, summary, description):
    logger.info('No issue found. Issue being created in project "%s" of type "%s" for "%s".' % (project, jira_config.get('issue_type', 'Task'), team))

    return jira.create_issue({
        'project': {'key': project},
        'summary': summary,
        'description': "%s\n\n%s" % (description_boundary, description),
        'issuetype': {'name': jira_config.get('issue_type', 'Task')},
        'labels': ['alert', team],
    })

@app.route('/-/health')
def health():
    return "OK", 200

@request_time.time()
@app.route('/issues/<project>/<team>', methods=['POST'])
def file_issue(project, team):
    """
    This endpoint accepts a JSON encoded notification according to the version 3 or 4
    of the generic webhook of the Prometheus Alertmanager.
    """
    logger.info('Update received from Alertmanager. Updating "%s"' % project)

    issue_states = {
        "alert": jira_config.get('issue_open', 'Open'),
        "resolved": jira_config.get('issue_closed', 'Closed')
    }

    # Order for the search query is important for the query performance. It relies
    # on the 'alert_group_key' field in the description that must not be modified.

    data = request.get_json()
    if data['version'] not in ["3", "4"]:
        return "unknown message version %s" % data['version'], 400

    resolved = data['status'] == "resolved"

    # In python 3, base64 must be passed a byte array. However, the supplied argument is
    # a string, unencoded. Thus, it must be converted to an encoded string, base64 encoded, and decoded again
    # for use by jinja
    data['jiraReference'] = base64.b64encode(data['groupKey'].encode('utf-8')).decode('utf-8')

    description = description_tmpl.render(data)
    summary = summary_tmpl.render(data)

    # If there's already a ticket for the incident, update it and reopen/close if necessary.
    issue = query(data['jiraReference'])
    if issue:
        # We have to check the available transitions for the issue. These differ
        # between boards depending on setup.
        trans = {}

        for t in transitions(issue):
          trans[t['name'].lower()] = t['id']

        if resolved:
            if issue_states['resolved'] in trans:
                close(issue, trans[issue_states['resolved']])
            else:
                logger.warning('The state "%s" is not a valid transition. Valid ones are "%s"' % (issue_states['resolved'], trans))
        elif issue.fields.status.name.lower() == jira_config.get('issue_closed', 'Closed'):
            if issue_states['alert'] in trans:
                reopen(issue, trans[issue_states['alert']])
            else:
                logger.warning('The state "%s" is not a valid transition. Valid ones are "%s"' % (issue_states['alert'], trans))

        update_issue(issue, summary, description)

    # Do not create an issue for resolved incidents that were never filed.
    elif not resolved:
        create_issue(project, team, summary, description)

    return "", 200


@app.route('/metrics')
def metrics():
    resp = make_response(prometheus.generate_latest(prometheus.core.REGISTRY))
    resp.headers['Content-Type'] = prometheus.CONTENT_TYPE_LATEST
    return resp, 200


@click.command()
@click.option('--host', help='Host listen address')
@click.option('--port', '-p', default=9050, help='Listen port for the webhook')
@click.option('--debug', '-d', default=False, is_flag=True, help='Enable debug mode')
@click.option('--config-file', '-c', default='/etc/jiralerts/jiralerts.ini', help='The path to jiralerts.ini')
@click.argument('server')

def main(config_file, host, port, server, debug):
    global jira
    global config
    global jira_config

    setup_logging()

    config = configparser.ConfigParser()
    config.read(config_file)

    if not config.has_section('jira'):
        config['jira'] = {}

    jira_config = config['jira']

    logger.info('Issue type that will be created: ' + jira_config.get('issue_type', 'Task'))

    username = os.environ.get('JIRA_USERNAME')
    password = os.environ.get('JIRA_PASSWORD')

    if not username or not password:
        logger.warn('JIRA_USERNAME or JIRA_PASSWORD not set')
        sys.exit(2)

    jira = JIRA(basic_auth=(username, password), server=server, logging=debug)
    app.run(host=host, port=port, debug=debug)

def setup_logging():
    global logger

    # Set up logging
    log_format = '%(asctime)-15s %(clientip)s %(user)-8s %(message)s'
    logger = logging.getLogger('jiralerts')
    logger.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    f = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    sh.setFormatter(f)

    logger.addHandler(sh)

if __name__ == "__main__":
    main()

