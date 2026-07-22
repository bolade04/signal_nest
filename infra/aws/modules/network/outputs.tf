# outputs.tf — non-sensitive network outputs for downstream modules
#
# Only non-sensitive identifiers are exported. Subnet id lists are ordered by the
# sorted AZ list so ordering is stable across runs. No account id, ARN, NAT public
# IP, or other sensitive value is exposed.

output "vpc_id" {
  description = "ID of the staging VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "IPv4 CIDR block of the staging VPC."
  value       = aws_vpc.this.cidr_block
}

output "availability_zones" {
  description = "Sorted list of AZ names the subnets are placed in."
  value       = local.azs
}

output "public_subnet_ids" {
  description = "Public subnet IDs, ordered by sorted AZ name (for ALB / edge origin path)."
  value       = [for az in local.azs : aws_subnet.public[az].id]
}

output "private_subnet_ids" {
  description = "Private subnet IDs, ordered by sorted AZ name (for ECS tasks, RDS, ElastiCache subnet groups)."
  value       = [for az in local.azs : aws_subnet.private[az].id]
}

output "public_route_table_id" {
  description = "ID of the public route table."
  value       = aws_route_table.public.id
}

output "private_route_table_id" {
  description = "ID of the shared private route table (for future S3 gateway-endpoint association)."
  value       = aws_route_table.private.id
}
