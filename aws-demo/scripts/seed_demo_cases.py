"""Insert the synthetic QA billing profiles into the configured DynamoDB table."""

from __future__ import annotations

import argparse
import os
import sys

from lla_agent.persistence.dynamodb import DynamoDBRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table-name",
        default=os.getenv("DYNAMODB_CUSTOMER_TABLE", ""),
        help="Customer data table name (or DYNAMODB_CUSTOMER_TABLE).",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        help="AWS Region for the table.",
    )
    args = parser.parse_args()
    if not args.table_name:
        parser.error("provide --table-name or set DYNAMODB_CUSTOMER_TABLE")

    import boto3

    dynamodb = boto3.resource("dynamodb", region_name=args.region) if args.region else boto3.resource("dynamodb")
    created = DynamoDBRepository(args.table_name, dynamodb=dynamodb).seed_demo_data()
    print(f"Se insertaron {created} registros sintéticos nuevos; los existentes se conservaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
