# README

## Quickstart

```
JIRA_USERNAME=<your_username> JIRA_PASSWORD=<your_password> ./main.py 'https://<your_jira>'
```

In your Alertmanager receiver configurations:

```yaml
receivers:
- name: 'jira_issues'
  webhook_configs:
  - url: 'http://<jiralerts_address>/issues/<jira_project>/<team>'
```

A typical usage could be a single 'ALERTS' projects where the `<team>`in the URL
refers to the affected system or the team that should handle the issue.

## Thanks

- fabxc is the primary author of all of this work. It has been forked for Sitewards use.
