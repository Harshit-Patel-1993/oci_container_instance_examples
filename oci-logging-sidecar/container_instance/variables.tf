variable "tenancy_ocid" {
  description = "Tenancy OCID. Also used as the compartment_id for IAM resources such as the dynamic group and policy."
  type        = string
}

variable "compartment_id" {
  description = "Compartment OCID for the network, logging resources, and container instance."
  type        = string
}

variable "region" {
  description = "OCI region for the provider."
  type        = string
}

variable "availability_domain" {
  description = "Availability domain for the container instance."
  type        = string
}

variable "display_name" {
  description = "Display name for the container instance."
  type        = string
  default     = "oci-logging-test"
}

variable "vcn_cidr_block" {
  description = "CIDR block for the VCN."
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidr_block" {
  description = "CIDR block for the subnet."
  type        = string
  default     = "10.0.0.0/24"
}

variable "assign_public_ip" {
  description = "Whether to assign a public IP to the container instance VNIC."
  type        = bool
  default     = true
}

variable "generator_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the generator HTTP port. Leave empty to disable ingress."
  type        = list(string)
  default     = []
}

variable "generator_http_port" {
  description = "HTTP port exposed by the generator container."
  type        = number
  default     = 8080
}

variable "shape" {
  description = "Container instance shape."
  type        = string
  default     = "CI.Standard.E4.Flex"
}

variable "shape_ocpus" {
  description = "OCPUs for the container instance shape config."
  type        = number
  default     = 1
}

variable "shape_memory_in_gbs" {
  description = "Memory in GB for the container instance shape config."
  type        = number
  default     = 16
}

variable "container_restart_policy" {
  description = "Restart policy for all containers in the container instance."
  type        = string
  default     = "NEVER"
}

variable "generator_image_url" {
  description = "OCIR or registry image URL for the log generator."
  type        = string
}

variable "forwarder_image_url" {
  description = "OCIR or registry image URL for the log forwarder."
  type        = string
}

variable "log_file_path" {
  description = "Shared log file path inside both containers."
  type        = string
  default     = "/mnt/logs/app.log"
}

variable "generator_default_log_level" {
  description = "Default level emitted by the generator when callers do not specify one."
  type        = string
  default     = "INFO"
}

variable "forwarder_log_level" {
  description = "Log level for the forwarder container."
  type        = string
  default     = "INFO"
}

variable "forwarder_flush_interval" {
  description = "Batch flush interval for the forwarder."
  type        = string
  default     = "5s"
}

variable "forwarder_chunk_limit_size" {
  description = "Maximum on-disk spool batch size before the forwarder forces a send."
  type        = string
  default     = "1m"
}

variable "forwarder_queued_chunks_limit_size" {
  description = "Maximum number of queued on-disk batches before the forwarder pauses reads."
  type        = string
  default     = "64"
}

variable "forwarder_disk_usage_log_interval" {
  description = "How often the forwarder logs total size of the source log files, including rotated siblings."
  type        = string
  default     = "5m"
}

variable "logrotate_frequency" {
  description = "Logrotate cadence keyword."
  type        = string
  default     = "hourly"
}

variable "logrotate_size" {
  description = "Rotate once the log file reaches this size."
  type        = string
  default     = "50M"
}

variable "logrotate_rotate_count" {
  description = "Number of rotated files to retain."
  type        = string
  default     = "24"
}

variable "logrotate_interval_seconds" {
  description = "How often the forwarder invokes logrotate."
  type        = string
  default     = "60"
}

variable "log_group_display_name" {
  description = "Display name for the OCI log group."
  type        = string
  default     = "oci-logging-test-log-group"
}

variable "log_group_description" {
  description = "Description for the OCI log group."
  type        = string
  default     = "Log group for the container instance logging test."
}

variable "custom_log_display_name" {
  description = "Display name for the OCI custom log resource."
  type        = string
  default     = "oci-log-forwarder"
}

variable "custom_log_retention_duration" {
  description = "Retention duration for the OCI custom log in days, in 30-day increments."
  type        = number
  default     = 30
}

variable "dynamic_group_name" {
  description = "Name for the dynamic group used by the container instance resource principal."
  type        = string
  default     = "oci-log-forwarder-dg"
}

variable "dynamic_group_description" {
  description = "Description for the runtime dynamic group."
  type        = string
  default     = "Dynamic group for container instances that push logs with resource principals."
}

variable "policy_name" {
  description = "Name for the IAM policy that grants the runtime principal access."
  type        = string
  default     = "oci-log-forwarder-policy"
}

variable "policy_description" {
  description = "Description for the IAM policy."
  type        = string
  default     = "Allows the container instance runtime principal to pull images and push custom logs."
}

variable "freeform_tags" {
  description = "Freeform tags applied to created resources."
  type        = map(string)
  default     = {}
}
