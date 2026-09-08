#!/usr/bin/env bash
#
# Publish the UEBA console as a CloudFront demo.
#
#   deploy/cloudfront-deploy.sh --password <pw> [--user demo] [--profile snowbit-research]
#
# One distribution, two origins: a private S3 bucket holding ui/dist, and the API
# Lambda's Function URL signed with SigV4 via OAC. Basic auth runs at the edge.
# Idempotent: re-running updates in place. Remove it all with cloudfront-teardown.sh.
set -euo pipefail

PROFILE="${PROFILE:-snowbit-research}"
REGION="${REGION:-ap-south-1}"
AUTH_USER="${AUTH_USER:-demo}"
AUTH_PASS="${AUTH_PASS:-}"
SKIP_BUILD="${SKIP_BUILD:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)  PROFILE="$2";   shift 2 ;;
    --region)   REGION="$2";    shift 2 ;;
    --user)     AUTH_USER="$2"; shift 2 ;;
    --password) AUTH_PASS="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$AUTH_PASS" ]]; then
  echo "--password is required: the demo serves real customer data." >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
AWS=(aws --profile "$PROFILE" --region "$REGION")
CF=(aws --profile "$PROFILE" --region us-east-1)   # CloudFront is global
ACCOUNT="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"

BUCKET="ueba-ui-demo-${ACCOUNT}-ap2"
API_FN="ueba-ui-api-ap2"
FUNC_NAME="ueba-demo-basic-auth"
OAC_S3="ueba-ui-demo-s3"
OAC_LAMBDA="ueba-ui-demo-lambda"
CACHE_POLICY="ueba-demo-api"
COMMENT="ueba-ui-demo"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ------------------------------------------------------------------ inputs ---
KEY_FILE="$ROOT/lambdas/api/.api-key"
[[ -f "$KEY_FILE" ]] || { echo "missing $KEY_FILE — run lambdas/api/deploy.sh first" >&2; exit 1; }
API_KEY="$(cat "$KEY_FILE")"

API_URL="$("${AWS[@]}" lambda get-function-url-config --function-name "$API_FN" \
  --query FunctionUrl --output text)"
API_HOST="$(sed -E 's#^https://##; s#/$##' <<<"$API_URL")"

# Build here rather than trusting whatever ui/dist happens to hold: shipping a
# stale bundle looks exactly like a caching bug from the browser side.
if [[ "$SKIP_BUILD" == "1" ]]; then
  [[ -f "$ROOT/ui/dist/index.html" ]] || { echo "no build — run 'cd ui && npm run build'" >&2; exit 1; }
else
  say "build ui"
  ( cd "$ROOT/ui" && npm run build )
fi

say "account $ACCOUNT · bucket $BUCKET · api origin $API_HOST"

# ------------------------------------------------------------------ bucket ---
say "S3 bucket"
if ! "${AWS[@]}" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  "${AWS[@]}" s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  echo "created"
else
  echo "already present"
fi
"${AWS[@]}" s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false" >/dev/null

# Old hashed assets are kept, not deleted at deploy time, so a browser holding a
# previous index.html can still load what it points at. This expires whatever has
# not been re-uploaded for 30 days. Safe only because the upload block below
# re-PUTs every current asset on every deploy, refreshing its LastModified.
"${AWS[@]}" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration "$(cat <<'JSON'
{"Rules":[{
  "ID":"expire-superseded-assets",
  "Status":"Enabled",
  "Filter":{"Prefix":"assets/"},
  "Expiration":{"Days":30},
  "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}
JSON
)" >/dev/null

# --------------------------------------------------------------------- OACs ---
oac_id() {
  "${CF[@]}" cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$1'].Id | [0]" --output text 2>/dev/null
}
ensure_oac() {
  local name="$1" type="$2" id
  id="$(oac_id "$name")"
  if [[ "$id" == "None" || -z "$id" ]]; then
    id="$("${CF[@]}" cloudfront create-origin-access-control \
      --origin-access-control-config \
      "Name=$name,Description=UEBA demo,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=$type" \
      --query 'OriginAccessControl.Id' --output text)"
  fi
  echo "$id"
}
say "origin access controls"
OAC_S3_ID="$(ensure_oac "$OAC_S3" s3)"
OAC_LAMBDA_ID="$(ensure_oac "$OAC_LAMBDA" lambda)"
echo "s3=$OAC_S3_ID lambda=$OAC_LAMBDA_ID"

# ---------------------------------------------------------------- function ---
say "basic-auth function"
EXPECTED="$(printf '%s' "$AUTH_USER:$AUTH_PASS" | base64)"
TMP_FN="$(mktemp -t basic-auth)"
sed "s/__EXPECTED__/$EXPECTED/" "$HERE/basic-auth.js" > "$TMP_FN"

if "${CF[@]}" cloudfront describe-function --name "$FUNC_NAME" >/dev/null 2>&1; then
  ETAG="$("${CF[@]}" cloudfront describe-function --name "$FUNC_NAME" --query ETag --output text)"
  "${CF[@]}" cloudfront update-function --name "$FUNC_NAME" --if-match "$ETAG" \
    --function-config "Comment=UEBA demo basic auth,Runtime=cloudfront-js-2.0" \
    --function-code "fileb://$TMP_FN" >/dev/null
else
  "${CF[@]}" cloudfront create-function --name "$FUNC_NAME" \
    --function-config "Comment=UEBA demo basic auth,Runtime=cloudfront-js-2.0" \
    --function-code "fileb://$TMP_FN" >/dev/null
fi
ETAG="$("${CF[@]}" cloudfront describe-function --name "$FUNC_NAME" --query ETag --output text)"
FUNC_ARN="$("${CF[@]}" cloudfront publish-function --name "$FUNC_NAME" --if-match "$ETAG" \
  --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)"
rm -f "$TMP_FN"
echo "$FUNC_ARN"

# ------------------------------------------------------------ cache policy ---
# Managed CachingDisabled forwards no Accept-Encoding, so API responses would ship
# uncompressed at 6.1 MB per dashboard load. This one compresses, keys on the query
# string, and holds 300s — the detector only rebuilds the snapshot every 2h.
say "cache policy"
POLICY_ID="$("${CF[@]}" cloudfront list-cache-policies --type custom \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='$CACHE_POLICY'].CachePolicy.Id | [0]" \
  --output text 2>/dev/null || echo None)"
if [[ "$POLICY_ID" == "None" || -z "$POLICY_ID" ]]; then
  POLICY_ID="$("${CF[@]}" cloudfront create-cache-policy --cache-policy-config "$(cat <<JSON
{
  "Name": "$CACHE_POLICY",
  "Comment": "UEBA demo API: compress, key on query string, 5 min",
  "DefaultTTL": 300, "MinTTL": 0, "MaxTTL": 300,
  "ParametersInCacheKeyAndForwardedToOrigin": {
    "EnableAcceptEncodingGzip": true,
    "EnableAcceptEncodingBrotli": true,
    "HeadersConfig": { "HeaderBehavior": "none" },
    "CookiesConfig": { "CookieBehavior": "none" },
    "QueryStringsConfig": { "QueryStringBehavior": "all" }
  }
}
JSON
)" --query 'CachePolicy.Id' --output text)"
fi
OPTIMIZED_ID="$("${CF[@]}" cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingOptimized'].CachePolicy.Id | [0]" --output text)"
echo "api=$POLICY_ID static=$OPTIMIZED_ID"

# ---------------------------------------------------------- distribution ---
DIST_ID="$("${CF[@]}" cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='$COMMENT'].Id | [0]" --output text 2>/dev/null || echo None)"

build_config() {
  local caller_ref="$1"
  python3 - "$caller_ref" <<PY
import json, sys
caller_ref = sys.argv[1]
cfg = {
  "CallerReference": caller_ref,
  "Comment": "$COMMENT",
  "Enabled": True,
  "PriceClass": "PriceClass_200",
  "DefaultRootObject": "index.html",
  # http2 only: enabling h3 makes CloudFront advertise alt-svc h3=":443", and Chrome
  # upgrading to QUIC mid-basic-auth fails the handshake with ERR_TOO_MANY_RETRIES.
  "HttpVersion": "http2",
  "Origins": {"Quantity": 2, "Items": [
    {"Id": "s3-ui",
     "DomainName": "$BUCKET.s3.$REGION.amazonaws.com",
     "OriginAccessControlId": "$OAC_S3_ID",
     "S3OriginConfig": {"OriginAccessIdentity": ""},
     "OriginPath": "", "CustomHeaders": {"Quantity": 0}},
    {"Id": "lambda-api",
     "DomainName": "$API_HOST",
     "OriginAccessControlId": "$OAC_LAMBDA_ID",
     "CustomOriginConfig": {
       "HTTPPort": 80, "HTTPSPort": 443,
       "OriginProtocolPolicy": "https-only",
       "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
       "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5},
     "OriginPath": "",
     "CustomHeaders": {"Quantity": 1, "Items": [
       {"HeaderName": "X-Api-Key", "HeaderValue": "$API_KEY"}]}}
  ]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-ui",
    "ViewerProtocolPolicy": "redirect-to-https",
    "CachePolicyId": "$OPTIMIZED_ID",
    "Compress": True,
    "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                       "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
    "FunctionAssociations": {"Quantity": 1, "Items": [
      {"FunctionARN": "$FUNC_ARN", "EventType": "viewer-request"}]}
  },
  "CacheBehaviors": {"Quantity": 1, "Items": [
    {"PathPattern": "/api/*",
     "TargetOriginId": "lambda-api",
     "ViewerProtocolPolicy": "redirect-to-https",
     "CachePolicyId": "$POLICY_ID",
     "Compress": True,
     "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
     "FunctionAssociations": {"Quantity": 1, "Items": [
       {"FunctionARN": "$FUNC_ARN", "EventType": "viewer-request"}]}}
  ]}
}
print(json.dumps(cfg))
PY
}

say "distribution"
if [[ "$DIST_ID" == "None" || -z "$DIST_ID" ]]; then
  CONFIG="$(build_config "ueba-demo-$(date +%s)")"
  DIST_ID="$("${CF[@]}" cloudfront create-distribution \
    --distribution-config "$CONFIG" --query 'Distribution.Id' --output text)"
  echo "created $DIST_ID"
else
  "${CF[@]}" cloudfront get-distribution-config --id "$DIST_ID" --output json > /tmp/ueba-live.json
  ETAG="$(python3 -c 'import json;print(json.load(open("/tmp/ueba-live.json"))["ETag"])')"
  CALLER_REF="$(python3 -c 'import json;print(json.load(open("/tmp/ueba-live.json"))["DistributionConfig"]["CallerReference"])')"
  build_config "$CALLER_REF" > /tmp/ueba-desired.json
  python3 - <<'MERGE'
import json
live = json.load(open("/tmp/ueba-live.json"))["DistributionConfig"]
want = json.load(open("/tmp/ueba-desired.json"))

# Deep merge: CloudFront rejects an update whose behaviors omit fields it defaulted
# in on create (SmoothStreaming, TrustedSigners, LambdaFunctionAssociations...), so
# overlay our keys onto the live objects instead of replacing them.
def overlay(base, patch):
    base = dict(base); base.update(patch); return base

for key in ("Comment", "Enabled", "PriceClass", "DefaultRootObject", "HttpVersion"):
    live[key] = want[key]

by_id = {o["Id"]: o for o in live["Origins"]["Items"]}
live["Origins"] = {"Quantity": len(want["Origins"]["Items"]),
                   "Items": [overlay(by_id.get(o["Id"], {}), o)
                             for o in want["Origins"]["Items"]]}

live["DefaultCacheBehavior"] = overlay(live["DefaultCacheBehavior"],
                                       want["DefaultCacheBehavior"])

by_path = {b["PathPattern"]: b for b in live.get("CacheBehaviors", {}).get("Items", [])}
items = []
for w in want["CacheBehaviors"]["Items"]:
    merged = overlay(by_path.get(w["PathPattern"], live["DefaultCacheBehavior"]), w)
    # An overlay cannot express a removal: drop anything the desired behavior omits.
    for droppable in ("OriginRequestPolicyId", "ForwardedValues", "MinTTL",
                      "DefaultTTL", "MaxTTL"):
        if droppable not in w:
            merged.pop(droppable, None)
    items.append(merged)
live["CacheBehaviors"] = {"Quantity": len(items), "Items": items}

json.dump(live, open("/tmp/ueba-merged.json", "w"))
MERGE
  "${CF[@]}" cloudfront update-distribution --id "$DIST_ID" \
    --distribution-config "file:///tmp/ueba-merged.json" --if-match "$ETAG" >/dev/null
  rm -f /tmp/ueba-live.json /tmp/ueba-desired.json /tmp/ueba-merged.json
  echo "updated $DIST_ID"
fi
DIST_ARN="arn:aws:cloudfront::$ACCOUNT:distribution/$DIST_ID"
DOMAIN="$("${CF[@]}" cloudfront get-distribution --id "$DIST_ID" \
  --query 'Distribution.DomainName' --output text)"

# --------------------------------------------------------------- policies ---
say "bucket policy + lambda permission"
"${AWS[@]}" s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Sid":"AllowCloudFrontRead","Effect":"Allow",
  "Principal":{"Service":"cloudfront.amazonaws.com"},
  "Action":"s3:GetObject","Resource":"arn:aws:s3:::$BUCKET/*",
  "Condition":{"StringEquals":{"AWS:SourceArn":"$DIST_ARN"}}}]}
JSON
)"
# OAC needs both: InvokeFunctionUrl for the URL front door, InvokeFunction for the
# invoke itself. With only the first, Lambda answers every signed request Forbidden.
"${AWS[@]}" lambda add-permission --function-name "$API_FN" \
  --statement-id AllowCloudFrontInvokeUrl \
  --action lambda:InvokeFunctionUrl \
  --principal cloudfront.amazonaws.com \
  --source-arn "$DIST_ARN" \
  --function-url-auth-type AWS_IAM >/dev/null 2>&1 || echo "url permission already present"
"${AWS[@]}" lambda add-permission --function-name "$API_FN" \
  --statement-id AllowCloudFrontInvokeFunction \
  --action lambda:InvokeFunction \
  --principal cloudfront.amazonaws.com \
  --source-arn "$DIST_ARN" >/dev/null 2>&1 || echo "invoke permission already present"

# ----------------------------------------------------------------- upload ---
say "upload ui/dist"
# Order matters: assets must exist before the index.html that names them, and
# nothing is deleted from assets/ — a browser mid-session is still holding the
# previous index and will ask for the previous hashes. Lifecycle reaps them.
# cp, not sync: sync skips unchanged files by size+mtime and so never reapplies
# --cache-control, letting headers drift silently across deploys.
"${AWS[@]}" s3 cp "$ROOT/ui/dist/assets/" "s3://$BUCKET/assets/" --recursive \
  --cache-control 'public,max-age=31536000,immutable' --only-show-errors

# Unhashed side files (icons). Short TTL, revalidated; safe to mirror-delete.
# The excludes cover the destination listing too, so neither assets/ nor the HTML
# below can be deleted by this pass.
"${AWS[@]}" s3 sync "$ROOT/ui/dist/" "s3://$BUCKET/" --delete \
  --exclude 'assets/*' --exclude '*.html' \
  --cache-control 'public,max-age=3600' --only-show-errors

# index.html last, and never cached by the browser: it is the only file that names
# the hashed bundles, so a browser that caches it cannot be reached by any
# CloudFront invalidation. max-age=0 still lets CloudFront hold and revalidate
# against the S3 ETag, so this costs one conditional GET per edge, not per user.
"${AWS[@]}" s3 cp "$ROOT/ui/dist/" "s3://$BUCKET/" --recursive \
  --exclude '*' --include '*.html' \
  --content-type 'text/html; charset=utf-8' \
  --cache-control 'public,max-age=0,must-revalidate' --only-show-errors

# Narrow paths: hashed assets are immutable and never need eviction; the entry
# point is the only thing that could be stale at the edge.
INV_ID="$("${CF[@]}" cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths '/' '/index.html' '/favicon*' '/apple-touch-icon.png' \
  --query 'Invalidation.Id' --output text)"
"${CF[@]}" cloudfront wait invalidation-completed \
  --distribution-id "$DIST_ID" --id "$INV_ID"

cat <<EOF

  URL       : https://$DOMAIN/
  login     : $AUTH_USER / (the password you passed)
  dist id   : $DIST_ID
  bucket    : s3://$BUCKET

  First deploy takes ~10 min to reach every edge. Check with:
    aws --profile $PROFILE --region us-east-1 cloudfront get-distribution \\
      --id $DIST_ID --query 'Distribution.Status' --output text

  Remove everything: deploy/cloudfront-teardown.sh --profile $PROFILE
EOF
