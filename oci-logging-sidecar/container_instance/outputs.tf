output "container_instance_id" {
  description = "OCID of the created container instance."
  value       = oci_container_instances_container_instance.logging_test.id
}

output "container_instance_state" {
  description = "Lifecycle state of the container instance."
  value       = oci_container_instances_container_instance.logging_test.state
}

output "subnet_id" {
  description = "OCID of the created subnet."
  value       = oci_core_subnet.logging_test.id
}

output "vcn_id" {
  description = "OCID of the created VCN."
  value       = oci_core_vcn.logging_test.id
}

output "dynamic_group_name" {
  description = "Name of the runtime dynamic group."
  value       = oci_identity_dynamic_group.forwarder_runtime.name
}

output "policy_name" {
  description = "Name of the runtime IAM policy."
  value       = oci_identity_policy.forwarder_runtime.name
}

output "log_group_id" {
  description = "OCID of the OCI log group."
  value       = oci_logging_log_group.forwarder.id
}

output "custom_log_id" {
  description = "OCID of the OCI custom log used by the forwarder."
  value       = oci_logging_log.forwarder.id
}

