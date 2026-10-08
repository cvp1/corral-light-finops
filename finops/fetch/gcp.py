"""Google Cloud billing export in BigQuery: one SQL query (standard SQL)
summing daily cost net of credits, by currency. Light's fetch proxy holds
the service account key, signs for the read-only token and adds it to each
request (plan §6.7.2); FinOps never sees the key or the token.

Grant parameters (non-secret, set with `corral-light module grant`):
  table     project.dataset.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX (required)
  project   the project that runs the query (default: the table's project)
  location  the dataset's location, when outside the US and EU multi-regions
"""
import re
import urllib.parse

from finops.fetch.common import Days, dec, result

BQ = "https://bigquery.googleapis.com/bigquery/v2"
TABLE_RE = re.compile(r"^([a-z][a-z0-9-]{4,61}[a-z0-9])\.([A-Za-z0-9_]{1,1024})\."
                      r"([A-Za-z0-9_]{1,1024})$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
LOCATION_RE = re.compile(r"^[A-Za-z0-9-]{2,40}$")
SQL = ("SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time)) AS day, currency, "
       "CAST(SUM(IFNULL(CAST(cost AS NUMERIC), 0)) + SUM(IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) "
       "FROM UNNEST(credits) c), 0)) AS STRING) AS net "
       "FROM `{table}` WHERE DATE(usage_start_time) >= @start AND DATE(usage_start_time) < @end "
       "GROUP BY day, currency ORDER BY day")


def fetch(client, start, end, params=None):
    params = params or {}
    m = TABLE_RE.match(params.get("table") or "")
    if not m:
        raise ValueError("the grant needs --param table=PROJECT.DATASET."
                         "gcp_billing_export_v1_<ACCOUNT> (letters, digits, _ and -)")
    project = params.get("project") or m.group(1)
    if not PROJECT_RE.match(project):
        raise ValueError("--param project is not a Google Cloud project id")
    location = params.get("location")
    if location is not None and not LOCATION_RE.match(location):
        raise ValueError("--param location is not a location name")
    h = {}                       # Light's fetch proxy adds the token
    body = {"query": SQL.format(table=params["table"]), "useLegacySql": False,
            "timeoutMs": 20000, "maxResults": 1000, "parameterMode": "NAMED",
            "queryParameters": [
                {"name": "start", "parameterType": {"type": "DATE"},
                 "parameterValue": {"value": start.isoformat()}},
                {"name": "end", "parameterType": {"type": "DATE"},
                 "parameterValue": {"value": end.isoformat()}}]}
    if location:
        body["location"] = location
    doc = client.json("POST", f"{BQ}/projects/{project}/queries", h, body=body)
    days = Days()
    for _ in range(200):
        if not isinstance(doc, dict):
            raise ValueError("the query response is not an object")
        if doc.get("errors"):
            raise ValueError("BigQuery reported query errors")
        if doc.get("jobComplete"):
            for row in doc.get("rows") or []:
                f = [c.get("v") for c in row.get("f", [])]
                if len(f) == 3 and f[0] and f[1] and f[2] is not None:
                    days.add(f[0], f[1], dec(f[2]))
        token_page = doc.get("pageToken")
        if doc.get("jobComplete") and not token_page:
            break
        ref = doc.get("jobReference") or {}
        job = ref.get("jobId")
        if not isinstance(job, str) or not re.match(r"^[A-Za-z0-9_-]{1,1024}$", job):
            raise ValueError("BigQuery returned no usable job id")
        q = {"maxResults": "1000", "timeoutMs": "20000"}
        if ref.get("location"):
            q["location"] = ref["location"]
        if token_page:
            q["pageToken"] = token_page
        doc = client.json("GET", f"{BQ}/projects/{project}/queries/{job}?"
                          + urllib.parse.urlencode(q), h)
    else:
        raise ValueError("the query did not finish after 200 polls")
    return result("gcp", start, end, days, {"id": params["table"].split(".")[0], "name": ""},
                  notes=["days are UTC dates of usage start; Google's invoice months "
                         "use Pacific time, so totals near a month boundary can differ"])
