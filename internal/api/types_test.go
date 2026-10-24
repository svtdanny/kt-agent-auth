package api

import "testing"

func TestPodIdentityParsing(t *testing.T) {
	id := "019a132e-2e49-7000-8000-000000000001"
	base := "spiffe://auth-demo.local/ns/demo/pod/" + id
	if ns, got, err := ParsePod(base); err != nil || ns != "demo" || got != id {
		t.Fatalf("%s %s %v", ns, got, err)
	}
	for _, bad := range []string{base + "/extra", base + "?forged=true", base + "#suffix", "spiffe://attacker/ns/demo/pod/" + id, "spiffe://auth-demo.local/ns/demo/pod/%30" + id[1:]} {
		if _, _, err := ParsePod(bad); err == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
}

func TestActorIdentityParsing(t *testing.T) {
	id := "019a132e-2e49-7000-8000-000000000001"
	base := "spiffe://substrate-actor.local/worker-for-actor/demo/session-" + id
	if ns, got, err := ParseActor(base); err != nil || ns != "demo" || got != id {
		t.Fatalf("%s %s %v", ns, got, err)
	}
	for _, bad := range []string{base + "/extra", base + "?forged=true", base + "#suffix", "spiffe://attacker/worker-for-actor/demo/session-" + id, "spiffe://substrate-actor.local/actor/demo/session-" + id, "spiffe://substrate-actor.local/worker-for-actor/demo/session-%30" + id[1:]} {
		if _, _, err := ParseActor(bad); err == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
}
