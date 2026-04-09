provider "oci" {
  region = var.region
}

locals {
  name_prefix = replace(var.display_name, "_", "-")
}

resource "oci_core_vcn" "logging_test" {
  compartment_id = var.compartment_id
  cidr_block     = var.vcn_cidr_block
  display_name   = "${local.name_prefix}-vcn"
  dns_label      = "logtestvcn"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_internet_gateway" "logging_test" {
  compartment_id = var.compartment_id
  display_name   = "${local.name_prefix}-igw"
  enabled        = true
  vcn_id         = oci_core_vcn.logging_test.id
  freeform_tags  = var.freeform_tags
}

resource "oci_core_route_table" "logging_test" {
  compartment_id = var.compartment_id
  display_name   = "${local.name_prefix}-rt"
  vcn_id         = oci_core_vcn.logging_test.id
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.logging_test.id
  }
}

resource "oci_core_security_list" "logging_test" {
  compartment_id = var.compartment_id
  display_name   = "${local.name_prefix}-sl"
  vcn_id         = oci_core_vcn.logging_test.id
  freeform_tags  = var.freeform_tags

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }

  dynamic "ingress_security_rules" {
    for_each = var.generator_ingress_cidrs
    content {
      protocol = "6"
      source   = ingress_security_rules.value

      tcp_options {
        min = var.generator_http_port
        max = var.generator_http_port
      }
    }
  }
}

resource "oci_core_subnet" "logging_test" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.logging_test.id
  cidr_block                 = var.subnet_cidr_block
  display_name               = "${local.name_prefix}-subnet"
  dns_label                  = "logtestsn"
  route_table_id             = oci_core_route_table.logging_test.id
  security_list_ids          = [oci_core_security_list.logging_test.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.freeform_tags
}

resource "oci_logging_log_group" "forwarder" {
  compartment_id = var.compartment_id
  display_name   = var.log_group_display_name
  description    = var.log_group_description
  freeform_tags  = var.freeform_tags
}

resource "oci_logging_log" "forwarder" {
  display_name       = var.custom_log_display_name
  log_group_id       = oci_logging_log_group.forwarder.id
  log_type           = "CUSTOM"
  is_enabled         = true
  retention_duration = var.custom_log_retention_duration
  freeform_tags      = var.freeform_tags
}

resource "oci_identity_dynamic_group" "forwarder_runtime" {
  compartment_id = var.tenancy_ocid
  name           = var.dynamic_group_name
  description    = var.dynamic_group_description
  matching_rule  = "ALL {resource.type = 'computecontainerinstance', resource.compartment.id = '${var.compartment_id}'}"
  freeform_tags  = var.freeform_tags
}

resource "oci_identity_policy" "forwarder_runtime" {
  compartment_id = var.tenancy_ocid
  name           = var.policy_name
  description    = var.policy_description
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.forwarder_runtime.name} to read repos in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.forwarder_runtime.name} to use log-content in compartment id ${var.compartment_id} where target.loggroup.id = '${oci_logging_log_group.forwarder.id}'",
  ]
}

resource "time_sleep" "before_container_instance" {
  create_duration = "180s"

  depends_on = [
    oci_core_subnet.logging_test,
    oci_logging_log_group.forwarder,
    oci_logging_log.forwarder,
    oci_identity_dynamic_group.forwarder_runtime,
    oci_identity_policy.forwarder_runtime,
  ]
}

resource "oci_container_instances_container_instance" "logging_test" {
  availability_domain      = var.availability_domain
  compartment_id           = var.compartment_id
  display_name             = var.display_name
  container_restart_policy = var.container_restart_policy
  shape                    = var.shape
  state                    = "ACTIVE"
  freeform_tags            = var.freeform_tags

  shape_config {
    ocpus         = var.shape_ocpus
    memory_in_gbs = var.shape_memory_in_gbs
  }

  containers {
    display_name = "oci-log-generator"
    image_url    = var.generator_image_url

    environment_variables = {
      LOG_FILE_PATH     = var.log_file_path
      HTTP_PORT         = tostring(var.generator_http_port)
      DEFAULT_LOG_LEVEL = var.generator_default_log_level
    }

    volume_mounts {
      mount_path  = "/mnt/logs"
      volume_name = "logs"
    }
  }

  containers {
    display_name                   = "oci-log-forwarder"
    image_url                      = var.forwarder_image_url
    is_resource_principal_disabled = false

    environment_variables = {
      LOG_FILE_PATH                    = var.log_file_path
      OCI_LOG_OBJECT_ID                = oci_logging_log.forwarder.id
      OCI_AUTH_TYPE                    = "resource_principal"
      LOG_FORWARDER_LOG_LEVEL          = var.forwarder_log_level
      LOG_FORWARDER_FLUSH_INTERVAL     = var.forwarder_flush_interval
      LOG_FORWARDER_CHUNK_LIMIT_SIZE   = var.forwarder_chunk_limit_size
      LOG_FORWARDER_QUEUED_BATCH_LIMIT = var.forwarder_queued_chunks_limit_size
      LOGROTATE_ENABLED                = tostring(var.logrotate_enabled)
      LOG_FORWARDER_DISK_USAGE_LOG_INTERVAL = var.forwarder_disk_usage_log_interval
      LOGROTATE_FREQUENCY              = var.logrotate_frequency
      LOGROTATE_SIZE                   = var.logrotate_size
      LOGROTATE_ROTATE_COUNT           = var.logrotate_rotate_count
      LOGROTATE_INTERVAL_SECONDS       = var.logrotate_interval_seconds
    }

    volume_mounts {
      mount_path  = "/mnt/logs"
      volume_name = "logs"
    }

    volume_mounts {
      mount_path  = "/var/lib/oci-log-forwarder"
      volume_name = "forwarder-state"
    }
  }

  vnics {
    subnet_id              = oci_core_subnet.logging_test.id
    display_name           = "${local.name_prefix}-vnic"
    hostname_label         = "logtest"
    is_public_ip_assigned  = var.assign_public_ip
    skip_source_dest_check = false
  }

  volumes {
    name        = "logs"
    volume_type = "EMPTYDIR"
  }

  volumes {
    name        = "forwarder-state"
    volume_type = "EMPTYDIR"
  }

  depends_on = [
    time_sleep.before_container_instance,
  ]
}
