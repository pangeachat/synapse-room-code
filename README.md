# Synapse Room Code

Extends room to optionally have a secret code. Upon knocking with a valid code, user is invited to the room.

[![Linting and Tests](https://github.com/pangeachat/synapse-room-code/actions/workflows/ci.yml/badge.svg)](https://github.com/pangeachat/synapse-room-code/actions/workflows/ci.yml)

## Usage

Send a `POST` request to `/_synapse/client/pangea/v1/knock_with_code` with JSON body `{access_code: string}`. Access code must be 7 digit alphanumeric, with at least 1 digit in there. Response `200 OK` format: `{ message: string, rooms: list[string] }`.

Send a `GET` request to `/_synapse/client/pangea/v1/request_room_code` to obtain unique room code. Response format: `{ access_code: string }`

## Installation

From the virtual environment that you use for Synapse, install this module with:
```shell
pip install synapse-room-code
```
(If you run into issues, you may need to upgrade `pip` first, e.g. by running
`pip install --upgrade pip`)

Then alter your homeserver configuration, adding to your `modules` configuration:
```yaml
modules:
  - module: synapse_room_code.SynapseRoomCode
    config: {}
```


## Development

In a virtual environment with pip ≥ 21.1, run
```shell
pip install -e .[dev]
```

To run the unit tests, ensure you have `postgres` installed in your system. You can check this by running `which postgres` - if it shows a path to your `postgres` executable then it is ready. 

To actually run the unit test, you can either use:
```shell
tox -e py
```
or
```shell
trial tests
```

To run the linters and `mypy` type checker, use `./scripts-dev/lint.sh`.


## Releasing

The exact steps for releasing will vary; but this is an approach taken by the
Synapse developers (assuming a Unix-like shell):

 1. Set a shell variable to the version you are releasing (this just makes
    subsequent steps easier):
    ```shell
    version=X.Y.Z
    ```

 2. Update `setup.cfg` so that the `version` is correct.

 3. Stage the changed files and commit.
    ```shell
    git add -u
    git commit -m v$version -n
    ```

 4. Push your changes.
    ```shell
    git push
    ```

 5. When ready, create a signed tag for the release:
    ```shell
    git tag -s v$version
    ```
    Base the tag message on the changelog.

 6. Push the tag.
    ```shell
    git push origin tag v$version
    ```

 7. If applicable:
    Create a *release*, based on the tag you just pushed, on GitHub or GitLab.

 8. If applicable:
    Create a source distribution and upload it to PyPI:
    ```shell
    python -m build
    twine upload dist/synapse_room_code-$version*
    ```
