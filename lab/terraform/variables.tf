variable "aws_profile" {
  description = "AWS CLI profile. Empty uses the default credential chain."
  type        = string
  default     = ""
}

variable "region" {
  description = "AWS region (lab.py passes the one from lab.local.json or LAB_REGION)"
  type        = string
}

variable "name" {
  description = "Prefix for every resource; also the value of the project tag."
  type        = string
  default     = "ssisbliss-lab"
}

variable "ami_id" {
  description = "Golden AMI to launch. Empty launches the Windows base image (used when building a golden AMI)."
  type        = string
  default     = ""
}

variable "base_ami_parameter" {
  type    = string
  default = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

variable "lab_version" {
  description = "Key in versions.json this host represents, e.g. 2019-4470."
  type        = string
  default     = "base"
}

variable "instance_type" {
  type    = string
  default = "m6i.xlarge"
}

variable "volume_size_gb" {
  type    = number
  default = 150
}

variable "idle_stop_minutes" {
  description = "Stop the host after this long with average CPU under 4% (0 = never). A stopped host keeps its disk."
  type        = number
  default     = 120
}
