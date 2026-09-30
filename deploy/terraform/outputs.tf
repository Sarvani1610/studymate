output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "uploads_bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "db_endpoint" {
  value     = aws_db_instance.postgres.address
  sensitive = true
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "private_subnets" {
  value = module.vpc.private_subnets
}

output "app_security_group" {
  value = aws_security_group.app.id
}
