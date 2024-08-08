.. _installation:

Installation
============

This installation documentation assumes that you already have Wazuh installed.
If you do not already have OpenCTI installed, please follow the project's
:octid:`installation instructions (docker) <installation/#using-docker>`. If
you are running or planning to install OpenCTI manually, i.e. not in docker,
note that all of the installation documentation for this connector is written
for docker-compose.

Using the following as an example, add a *connector-wazuh* service to your
OpenCTI docker-compose file:

.. subst_literalinclude:: connector-compose-simple.yml
   :language: yaml
   :linenos:

.. warning::

   This is a bare-minimum example with several placeholder values. Look at the
   :ref:`Configuration <config>` chapter for how to configure the connector.

After starting OpenCTI, look for any errors (get a continuous log with a
little bit of history by running ``docker compose logs -f --tail=100
connector-wazuh``).

.. include:: alpha_warning.rst

.. _versioning:

Versioning
~~~~~~~~~~

The following tags are published to the docker registry:

.. list-table:: Connector docker tags
   :header-rows: 1

   * - Tag
     - Description
   * - x.y.z_æ
     - A connector (stable) release
   * - dev_æ
     - Latest development version (branch *dev*) of the connector
   * - latest
     - The lastest release (à la tag |latest|) of the connector)

Each tag (except "latest") has an OpenCTI version suffix, _æ, like
"|latest_octi_ver|", requiring you to specify which OpenCTI version you are
running. The connector is built against the versions listed in :ref:`this table
<version_compat>`.

Use a version like "|latest|" to reference a stable version of the connector.
Do not use *latest*, because you should really match your OpenCTI version
against the OpenCTI API used by the connector. "latest" refers to the latest
connector release built for the latest OpenCTI release (at the time of
development). See :ref:`versions <versions>` for more information.

See :ref:`versions and compatibilty <versions>` for how this project versions
its releases.

Creating users
~~~~~~~~~~~~~~

Before running the connector, you need to create an OpenCTI user and generate a
token, as well as create an OpenSearch read-only user:

.. toctree::
   :maxdepth: 2
   
   create_opensearch_user
   create_opencti_user

Finishing touches
~~~~~~~~~~~~~~~~~

In order to get the most out of OpenCTI along with this connector, you may want
to go through a few settings and customisation in OpenCTI:

.. toctree::
   :maxdepth: 2

   opencti_configuration
