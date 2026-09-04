# AWS production access

The application uses the AWS SDK credential chain. Do not deploy local AWS
credentials or place access keys in `.env`.

For the current EC2 deployment, attach an IAM role to the instance and add the
policy in `infra/dynamodb-task-policy.json` after replacing `REPLACE_ACCOUNT_ID`.

For ECS, attach the same policy to the ECS **task role**. Do not attach it only
to the task execution role: the execution role pulls images and writes logs,
while the task role authorises the running Flask application.

The policy intentionally excludes table scans, deletes and table-management
permissions. The application only needs keyed reads, conditional writes and
transactions on the `earnings-assistant` table in `ap-south-1`.
