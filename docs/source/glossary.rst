.. _glossary:

Glossary
===================================================

.. glossary::

   API
      Application programming interface

   AWS
      Amazon Web Services

   CTI
      Cyber threat intelligence

   docker
      Docker is a tool that simplifies the process of creating, deploying, and
      managing applications by packaging them with their dependencies into
      standardized units called containers.

   DSL
      Domain-specific language, or more specifically :term:`OpenSearch`'s
      :dsl:`query DSL <>` in the context of this connector.

   Enrichment
      In the contect of this connector, *enrichment* can mean both of the
      following:

      #. The OpenCTI concept of running an :octiu:`enrichment connector
         <enrichment>` to enrich an entity, typically an :term:`SCO`, with
         more information. This connector does not really do that, but chooses
         the :octid:`enrichment connector type <connectors/#enrichment>`,
         because it fits the most. The *enrichment* performed by running this
         connector, is searching for the entity in Wazuh and create sightings
         and incidents. Incidents, however, are packed with objects extracted
         as context from alerts. This is what this connector refers to as
         enrichment in its architecture:

      #. When an incident (and an incident response case) is created by this
         connector, as many entities as possible are created from the
         available information in the alerts returned by the search. This is
         the *enrichment* stage in the connector.

   FIM
      Wazuh's :wazuh:`File integrity monitoring (FIM) module
      <capabilities/file-integrity/index.html>`, also referred to as *syscheck*,
      creates events when files are created, modified and deleted.

   GCP
      Google Cloud Platform

   GDPR
      General Data Protection Regulation, an European Union regulation on
      information privacy in the EU and EEA (European Economic Area)

   hive
      A hive i a logical group of keys, subkeys and values in the Windows
      registry.

   IoC
      Indicator of compromise

   Marking definition
      Marking definition is a :stix:`STIX meta object <#_95gfoglikdzh>` used
      to segregate data in OpenCTI. The most common use case is to categorise
      and protect data based on its sensitivity and classification level. See
      the OpenCTI documentation on :octia:`data segregation <segregation>` for
      more information.


      In this connector, the following settings relating to marking
      definitions/TLP:

      :attr:`~wazuh.config.Config.max_tlp`
         
         The highest TLP this connector is allowed to look up. For instance,
         if max_tlp is set to TLP:AMBER, entities marked with TLP:RED will be
         ignored.

      :attr:`~wazuh.config.Config.tlps`

         This list of marking definitions will be set on every single entity
         produced by the connector (mainly through :ref:`enrichment
         <enrichment>`.

   OpenSearch
      OpenSearch is the default alert database, search engine and collection of
      dashboards used by :term:`Wazuh`, unless Elastic/Kibana is used.

   SCO
      :term:`STIX` cyber observable. See :ref:`observable <observable>` for
      details.

   SDO
      :term:`STIX` domain object

   SID
      Security Identifier, a unique identifier assigned to each security
      principal, such as a user, group or computer, in a Windows environment.

   SIEM
      Security information and event management

   SOC
      Security operations centre

   SRO
      :term:`STIX` relationship object

   STIX
      Structured Threat Information Expression, a language and serialisation
      format used to exchange cyber threat intelligence. STIX is used
      extensively in OpenCTI and is the main format used to import and export
      data.

      See `Introduction to STIX
      <https://oasis-open.github.io/cti-documentation/stix/intro>`_ and the
      :stix:`STIX reference <>` for details.

   TLP
      Traffic light protocol, the default :term:`marking definition` used in
      OpenCTI. See the OpenCTI documentation on :octia:`TLP in data
      seggregation <segregation/?h=traff#traffic-light-protocol>` for more
      information.

      See :term:`marking definition` for more information on how TLP is used
      in the connector.

   TTP
      Tactis, techniques and procedures, usually referring to `MITRE ATT&CK
      <https://attack.mitre.org/>`_

   UUID
      Universally Unique Identifier

   Wazuh
      An open-source :term:`SIEM`
