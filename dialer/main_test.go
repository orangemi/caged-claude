package main

import (
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestValidRouteAcceptsOnlyLiteralTargetsAndLocalListeners(t *testing.T) {
	good := []Route{
		{Listen: "127.0.0.1:43001", Targets: []string{"203.0.113.1:443", "203.0.113.2:443"}},
		{Listen: "unix:/state/.dialer/sock/a/dialer.sock", Targets: []string{"203.0.113.1:7210"}},
	}
	for _, route := range good {
		if err := validRoute(route); err != nil {
			t.Fatalf("%+v: %v", route, err)
		}
	}
	bad := []Route{
		{Listen: "0.0.0.0:43001", Targets: []string{"203.0.113.1:443"}},
		{Listen: "127.0.0.1:43001", Targets: []string{"node.example:443"}},
		{Listen: "127.0.0.1:43001", Targets: []string{"203.0.113.1:0"}},
		{Listen: "127.0.0.1:43001"},
		{Listen: "unix:relative.sock", Targets: []string{"203.0.113.1:443"}},
		{Listen: "127.0.0.1:43001", Targets: []string{"1.1.1.1:1", "1.1.1.1:2", "1.1.1.1:3", "1.1.1.1:4", "1.1.1.1:5"}},
	}
	for _, route := range bad {
		if validRoute(route) == nil {
			t.Fatalf("accepted %+v", route)
		}
	}
}

func TestReadRoutesDropsInvalidEntries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "routes.json")
	if routes, err := readRoutes(path); err != nil || len(routes.Instances) != 0 {
		t.Fatalf("missing file: %v %v", routes, err)
	}
	data, _ := json.Marshal(Routes{Instances: map[string]Route{
		"ok":  {Listen: "127.0.0.1:43001", Targets: []string{"203.0.113.1:443"}},
		"bad": {Listen: "0.0.0.0:43002", Targets: []string{"203.0.113.1:443"}},
	}})
	os.WriteFile(path, data, 0o600)
	routes, err := readRoutes(path)
	if err != nil || len(routes.Instances) != 1 || routes.Instances["ok"].Listen == "" {
		t.Fatalf("got %v %v", routes, err)
	}
}

func TestTraceField(t *testing.T) {
	if got := traceField("fl=1\nip=203.0.113.9\nloc=SG\n", "ip"); got != "203.0.113.9" {
		t.Fatal(got)
	}
}

// echo answers each connection with what it reads, prefixed by its own name.
func echo(t *testing.T, name string) string {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				data, _ := io.ReadAll(conn)
				conn.Write([]byte(name + ":" + string(data)))
			}()
		}
	}()
	return listener.Addr().String()
}

func roundTrip(t *testing.T, network, address, message string) string {
	var conn net.Conn
	var err error
	for attempt := 0; attempt < 50; attempt++ {
		if conn, err = net.Dial(network, address); err == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.Write([]byte(message))
	conn.(interface{ CloseWrite() error }).CloseWrite()
	reply, _ := io.ReadAll(conn)
	return string(reply)
}

type recorder struct {
	sync.Mutex
	dialed []string
}

func (r *recorder) list() string {
	r.Lock()
	defer r.Unlock()
	return strings.Join(r.dialed, ",")
}

func testServer(t *testing.T) (*server, *recorder) {
	s := newServer(t.TempDir())
	record := &recorder{}
	s.iface, s.ifaceAt = Iface{Name: "test0", Index: 1}, time.Now().Add(time.Hour)
	s.verify = func(*instance, Route) {}
	s.dial = func(iface Iface, target string) (net.Conn, error) {
		record.Lock()
		record.dialed = append(record.dialed, iface.Name+"->"+target)
		record.Unlock()
		return net.Dial("tcp", target)
	}
	return s, record
}

func counts(s *server, name string) (int64, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := s.instances[name].status
	return status.Connections, status.Failures
}

func TestForwardsOnlyToRouteTargetsInOrder(t *testing.T) {
	s, dialed := testServer(t)
	dead, _ := net.Listen("tcp", "127.0.0.1:0")
	deadAddress := dead.Addr().String()
	dead.Close()
	live := echo(t, "node")
	listen := freePort(t)
	s.reconcile(Routes{Instances: map[string]Route{"a": {Listen: listen, Targets: []string{deadAddress, live}, Revision: 1}}})
	if got := roundTrip(t, "tcp", listen, "hello"); got != "node:hello" {
		t.Fatalf("reply %q", got)
	}
	if got := dialed.list(); got != "test0->"+deadAddress+",test0->"+live {
		t.Fatalf("dialed %v", got)
	}
	if connections, failures := counts(s, "a"); connections != 1 || failures != 0 {
		t.Fatalf("connections %d failures %d", connections, failures)
	}
}

func TestUnixListenerAndRouteUpdatesAndRemoval(t *testing.T) {
	s, _ := testServer(t)
	first, second := echo(t, "first"), echo(t, "second")
	socket := filepath.Join(t.TempDir(), "sock", "dialer.sock")
	route := Route{Listen: "unix:" + socket, Targets: []string{first}, Revision: 1}
	s.reconcile(Routes{Instances: map[string]Route{"a": route}})
	if got := roundTrip(t, "unix", socket, "x"); got != "first:x" {
		t.Fatalf("reply %q", got)
	}
	route.Targets, route.Revision = []string{second}, 2
	s.reconcile(Routes{Instances: map[string]Route{"a": route}})
	if got := roundTrip(t, "unix", socket, "y"); got != "second:y" {
		t.Fatalf("reply after update %q", got)
	}
	s.reconcile(Routes{})
	if _, err := net.Dial("unix", socket); err == nil {
		t.Fatal("listener survived route removal")
	}
}

func TestNoInterfaceRefusesInsteadOfDialingUnbound(t *testing.T) {
	s, dialed := testServer(t)
	s.mu.Lock()
	s.ifaceErr = errNoInterface
	s.mu.Unlock()
	listen := freePort(t)
	s.reconcile(Routes{Instances: map[string]Route{"a": {Listen: listen, Targets: []string{echo(t, "node")}, Revision: 1}}})
	if got := roundTrip(t, "tcp", listen, "x"); got != "" {
		t.Fatalf("forwarded without an interface: %q", got)
	}
	if _, failures := counts(s, "a"); dialed.list() != "" || failures != 1 {
		t.Fatalf("dialed %v failures %d", dialed.list(), failures)
	}
}

func freePort(t *testing.T) string {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	return address
}

func TestRelayForwardsSocketsToTheWindowsDialerPort(t *testing.T) {
	windows := echo(t, "windows-dialer")
	socket := filepath.Join(t.TempDir(), "sock", "dialer.sock")
	routes := Routes{Instances: map[string]Route{
		"wsl":   {Listen: windows, Targets: []string{"203.0.113.1:443"}, Relay: "unix:" + socket, Revision: 1},
		"plain": {Listen: "127.0.0.1:43002", Targets: []string{"203.0.113.1:443"}, Revision: 1},
	}}
	relayed := relayRoutes(routes)
	if len(relayed.Instances) != 1 || relayed.Instances["wsl"].Targets[0] != windows {
		t.Fatalf("relayed %+v", relayed)
	}
	s := newRelay(t.TempDir())
	s.reconcile(relayed)
	// No physical interface is needed or looked up on the relay side.
	if got := roundTrip(t, "unix", socket, "x"); got != "windows-dialer:x" {
		t.Fatalf("reply %q", got)
	}
	if s.name() != "relay" || validRoute(Route{Listen: "127.0.0.1:1", Targets: []string{"1.1.1.1:1"}, Relay: "relative.sock"}) == nil {
		t.Fatal("relay name or relay path validation")
	}
}
