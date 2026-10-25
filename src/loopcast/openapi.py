"""Print the API contract without creating application runtime resources."""
import json
import sys

from .api import create_app


def main():
    json.dump(create_app(data_dir="/nonexistent/loopcast-openapi-no-runtime").openapi(), sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
