#!/usr/bin/env bash
# Build, push and roll out a new image to ECS.
#   ./deploy/deploy.sh v1.4.0
set -euo pipefail

TAG="${1:?usage: deploy.sh <image-tag>}"
REGION="${AWS_REGION:-us-west-2}"
CLUSTER="${CLUSTER:-studymate}"

cd "$(dirname "$0")/terraform"
REPO="$(terraform output -raw ecr_repository_url)"
cd - >/dev/null

echo "logging in to ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%/*}"

echo "building $REPO:$TAG"
docker build --platform linux/amd64 -t "$REPO:$TAG" .
docker push "$REPO:$TAG"

echo "applying terraform with image tag $TAG"
(cd deploy/terraform && terraform apply -auto-approve -var "image_tag=$TAG")

echo "running database setup as a one off task"
SUBNETS="$(cd deploy/terraform && terraform output -json private_subnets | jq -r 'join(",")')"
APP_SG="$(cd deploy/terraform && terraform output -raw app_security_group)"
aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition studymate-api \
  --overrides '{"containerOverrides":[{"name":"api","command":["flask","--app","wsgi","init-db"]}]}' \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$APP_SG],assignPublicIp=DISABLED}" \
  >/dev/null

echo "waiting for services to settle"
aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" --services studymate-api studymate-worker
echo "deployed $TAG"
