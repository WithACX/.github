# withACX defaults

Default community health files for A-CX repositories.

GitHub applies the issue and pull request templates in this repository to
any withACX repository that does not define its own. A repository with its
own `.github/ISSUE_TEMPLATE` folder or pull request template overrides
these defaults completely.

`profile/README.md` is the public profile shown at
[github.com/withACX](https://github.com/withACX).

## Scope of this repository

This repository carries default community health files and the org profile,
and nothing else. GitHub does not inherit workflows from an org `.github`
repository, so a workflow placed here reaches no other repository. Any
workflow that needs to run in a repository is committed to that repository;
anything that can work through the API instead runs once here, on a schedule,
against an explicit list of repositories.
