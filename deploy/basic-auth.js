// CloudFront Function, viewer-request. Runs before cache and origin, so an
// unauthenticated request never reaches S3 or the API Lambda.
// __EXPECTED__ is replaced at deploy time with base64("user:password").

var EXPECTED = "Basic __EXPECTED__";

function handler(event) {
  var headers = event.request.headers;

  if (headers.authorization && headers.authorization.value === EXPECTED) {
    // Strip it: OAC signs the origin request with SigV4 in this same header, and
    // a forwarded Basic credential would overwrite that signature.
    delete event.request.headers.authorization;
    return event.request;
  }

  return {
    statusCode: 401,
    statusDescription: "Unauthorized",
    headers: {
      "www-authenticate": { value: 'Basic realm="UEBA demo"' },
      "cache-control": { value: "no-store" },
    },
  };
}
