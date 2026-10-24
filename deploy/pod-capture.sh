#!/bin/sh
set -eu

: "${AUTH_EGRESS_IP:?dedicated gateway Service IP required}"
: "${AUTH_DNS_IP:?cluster DNS Service IP required}"

# Only this Pod's network namespace is modified. The application UID cannot
# impersonate the relay UID or alter these rules after dropping capabilities.
iptables -w -t nat -N AUTH_EGRESS
iptables -w -t nat -A AUTH_EGRESS -m owner --uid-owner 1337 -j RETURN
iptables -w -t nat -A AUTH_EGRESS -d 127.0.0.0/8 -j RETURN
iptables -w -t nat -A AUTH_EGRESS -p tcp --dport 53 -j RETURN
iptables -w -t nat -A AUTH_EGRESS -p tcp -j REDIRECT --to-ports 15001
iptables -w -t nat -A OUTPUT -p tcp -j AUTH_EGRESS

# This firewall is in the Pod netns and works even when the CNI ignores
# NetworkPolicy. After REDIRECT, application TCP reaches loopback. Only the
# relay UID may connect to the gateway; DNS goes only to the cluster resolver.
iptables -w -N AUTH_PROTOCOLS
iptables -w -A AUTH_PROTOCOLS -d 127.0.0.0/8 -j RETURN
# A redirected local socket can retain the application's Pod IP as its source.
# Permit the relay's return packets and replies to existing inbound connections.
iptables -w -A AUTH_PROTOCOLS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -w -A AUTH_PROTOCOLS -d "$AUTH_EGRESS_IP" -p tcp --dport 8443 -m owner --uid-owner 1337 -j RETURN
iptables -w -A AUTH_PROTOCOLS -d "$AUTH_DNS_IP" -p tcp --dport 53 -j RETURN
iptables -w -A AUTH_PROTOCOLS -d "$AUTH_DNS_IP" -p udp --dport 53 -j RETURN
iptables -w -A AUTH_PROTOCOLS -j REJECT
iptables -w -A OUTPUT -j AUTH_PROTOCOLS

# This fixture uses IPv4 destinations. Block non-loopback IPv6 in the selected
# Pod instead of leaving an untested second path around its capture rules.
ip6tables -w -N AUTH_EGRESS6
ip6tables -w -A AUTH_EGRESS6 -d ::1/128 -j RETURN
ip6tables -w -A AUTH_EGRESS6 -j REJECT
ip6tables -w -A OUTPUT -j AUTH_EGRESS6
