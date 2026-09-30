variable "project" {
  type    = string
  default = "studymate"
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "image_tag" {
  description = "Tag of the image in ECR to deploy"
  type        = string
  default     = "latest"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "api_max_count" {
  type    = number
  default = 6
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "llm_provider" {
  description = "stub, openai or bedrock"
  type        = string
  default     = "bedrock"
}

variable "certificate_arn" {
  description = "ACM certificate for the HTTPS listener. Leave empty to serve plain HTTP (demo only)."
  type        = string
  default     = ""
}
