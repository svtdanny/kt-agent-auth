-- This listener has no network socket. Only Envoy's ext_proc client reaches it.
-- The metadata is generated from verified Actor filter state, not HTTP headers.
function envoy_on_request(handle)
  local identity = handle:headers():get("x-auth-verified-actor") or ""
  local namespace, actor = string.match(identity,
    "^spiffe://substrate%-actor%.local/worker%-for%-actor/([^/]+)/([^/]+)$")
  if not namespace or #namespace > 63
      or not string.match(namespace, "^[a-z0-9][a-z0-9%-]*$")
      or not string.match(namespace, "[a-z0-9]$")
      or #actor ~= 44 or not string.match(actor, "^session%-[0-9a-f%-]+$")
      or string.gsub(string.sub(actor, 9), "%x", "") ~= "----"
      or string.sub(actor, 17, 17) ~= "-" or string.sub(actor, 22, 22) ~= "-"
      or string.sub(actor, 27, 27) ~= "-" or string.sub(actor, 32, 32) ~= "-" then
    handle:respond({[":status"] = "403"}, "invalid Actor identity")
    return
  end
  handle:headers():replace(":authority",
    "authorizer-extproc." .. namespace .. ".svc:50052")
  handle:headers():remove("x-auth-verified-actor")
end
