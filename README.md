# Quick Start

Ganache network provider plugin for Ape. Ganache is a tool for creating a local blockchain for fast Ethereum development.

## Dependencies

* [python3](https://www.python.org/downloads) version 3.8 or greater, python3-dev
* Node.js, NPM, and Ganache. See Ganache's [Installation](https://github.com/trufflesuite/ganache#command-line-use>) documentation for steps.

## Installation

### via ``pip``

You can install the latest release via [pip](https://pypi.org/project/pip/):

```bash
pip install ape-ganache
```

### via ``setuptools``

You can clone the repository and use [setuptools](https://github.com/pypa/setuptools) for the most up-to-date version:

```bash
git clone https://github.com/ApeWorX/ape-ganache.git
cd ape-ganache
python3 setup.py install
```

## Quick Usage

To use the plugin, first install Ganache locally into your Ape project directory:

```bash
cd your-ape-project
npm install --global ganache
```

After that, you can use the ``--network ethereum:development:ganache`` command line flag to use the ganache network (if it's not already configured as the default).

This network provider takes additional Ganache-specific configuration options. To use them, add these configs in your project's ``ape-config.yaml``:

```yaml
ganache:
  port: 8555
```

To select a random port, use a value of "auto":

```yaml
ganache:
  port: auto
```

This is useful for multiprocessing and starting up multiple providers.

## Mainnet Fork

The ``ape-ganache`` plugin also includes a mainnet fork provider. It requires using another provider that has access to mainnet.

Use it in most commands like this:

```bash
ape console --network :mainnet-fork:ganache
```

Specify the upstream archive-data provider in your ``ape-config.yaml``:

```yaml
ganache:
  mainnet_fork:
    upstream_provider: infura
```

Otherwise, it defaults to the default mainnet provider plugin. You can also specify a ``block_number``.

**NOTE**: Make sure you have the upstream provider plugin installed for ape.

```bash
ape plugins add infura
```

## Development

Please see the [contributing guide](CONTRIBUTING.md) to learn more how to contribute to this project.
Comments, questions, criticisms and pull requests are welcomed.
