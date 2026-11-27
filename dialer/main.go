// Command caged-dialer connects a cage's gateway to its pinned proxy node over
// the host's physical network interface, so that a host TUN or VPN (Clash,
// mihomo, sing-box, ...) cannot capture or reroute that connection.
//
// It forwards raw TCP only to the node addresses the launcher wrote into
// routes.json; the proxy protocol (and its TLS) stays end to end between the
// gateway's mihomo and the node. Connections are never dialed without the
// interface binding: without a usable physical interface they are refused.
//
//	caged-dialer serve --dir DIR           run until routes.json has been empty for a while
//	caged-dialer serve --relay --dir DIR   WSL side: relay each route's Unix socket to the
//	                                       Windows dialer's loopback port (needs mirrored networking)
//	caged-dialer version
//
// One process serves every instance of a project; a lock file in DIR keeps it
// single. ./cage starts it before the launcher; the launcher only edits
// routes.json and reads status.json.
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	version = "1"
	// An idle dialer (no routes) exits, so stopping needs no signals on any platform.
	idleExit    = 20 * time.Second
	startGrace  = 120 * time.Second
	dialTimeout = 5 * time.Second
	traceURL    = "https://1.1.1.1/cdn-cgi/trace"
	sampleIP    = "1.1.1.1"
)

// Route is one instance's entry in routes.json, written by the launcher.
type Route struct {
	Listen   string   `json:"listen"`  // "127.0.0.1:PORT" or "unix:/abs/path"
	Targets  []string `json:"targets"` // "ip:port", tried in order
	SNI      string   `json:"sni"`
	Revision int64    `json:"revision"`
	// Relay, on WSL with Docker Engine: the Unix socket the gateway dials. The
	// Linux-side relay forwards it to Listen, where the Windows dialer serves.
	Relay string `json:"relay,omitempty"`
}

type Routes struct {
	Instances map[string]Route `json:"instances"`
}

type Probe struct {
	OK    bool   `json:"ok"`
	Ms    int64  `json:"ms"`
	Error string `json:"error,omitempty"`
}

// Check compares the bound path with the host's default path to the node.
type Check struct {
	Revision    int64  `json:"revision"`
	Interface   string `json:"interface"`
	Bound       Probe  `json:"bound"`
	Default     Probe  `json:"default"`
	BoundExit   string `json:"bound_exit,omitempty"`
	DefaultExit string `json:"default_exit,omitempty"`
}

type InstanceStatus struct {
	Listen      string `json:"listen"`
	Listening   bool   `json:"listening"`
	ListenError string `json:"listen_error,omitempty"`
	Revision    int64  `json:"revision"`
	Check       *Check `json:"check,omitempty"`
	Connections int64  `json:"connections"`
	Failures    int64  `json:"failures"`
	LastError   string `json:"last_error,omitempty"`
}

type Status struct {
	Version        string                     `json:"version"`
	PID            int                        `json:"pid"`
	Heartbeat      int64                      `json:"heartbeat"`
	Interface      string                     `json:"interface,omitempty"`
	InterfaceError string                     `json:"interface_error,omitempty"`
	SampleRoute    string                     `json:"sample_route,omitempty"` // interface the host routes 1.1.1.1 to
	Tunnel         bool                       `json:"tunnel"`
	Instances      map[string]*InstanceStatus `json:"instances"`
}

type Iface struct {
	Name  string
	Index int
}

type instance struct {
	name     string
	route    atomic.Pointer[Route]
	listener net.Listener
	status   *InstanceStatus
}

type server struct {
	dir       string
	relay     bool // only forward each route's Relay socket to its Listen address
	mu        sync.Mutex
	instances map[string]*instance
	iface     Iface
	ifaceErr  error
	ifaceAt   time.Time
	sample    string
	tunnel    bool
	// dial is the bound dialer and verify the per-revision check; tests replace both.
	dial   func(iface Iface, target string) (net.Conn, error)
	verify func(i *instance, route Route)
}

func main() {
	log.SetFlags(log.LstdFlags)
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: caged-dialer serve --dir DIR | version")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version":
		fmt.Println(version)
	case "serve":
		flags := flag.NewFlagSet("serve", flag.ExitOnError)
		dir := flags.String("dir", "", "state directory holding routes.json")
		relay := flags.Bool("relay", false, "relay Unix sockets to the Windows dialer (WSL)")
		_ = flags.Parse(os.Args[2:])
		if *dir == "" {
			log.Fatal("--dir is required")
		}
		s := newServer(*dir)
		if *relay {
			s = newRelay(*dir)
		}
		unlock, err := lock(filepath.Join(*dir, s.name()+".lock"))
		if err != nil {
			log.Printf("another %s is already running for %s", s.name(), *dir)
			return
		}
		defer unlock()
		s.run()
	default:
		fmt.Fprintln(os.Stderr, "unknown command:", os.Args[1])
		os.Exit(2)
	}
}

func newServer(dir string) *server {
	s := &server{dir: dir, instances: map[string]*instance{}, dial: func(iface Iface, target string) (net.Conn, error) {
		return boundDialer(iface).Dial("tcp", target)
	}}
	s.verify = s.check
	return s
}

// newRelay hands connections to a local port without any interface binding:
// the Windows dialer behind that port binds the physical interface itself.
func newRelay(dir string) *server {
	s := &server{dir: dir, relay: true, instances: map[string]*instance{}, dial: func(_ Iface, target string) (net.Conn, error) {
		return (&net.Dialer{Timeout: dialTimeout}).Dial("tcp", target)
	}}
	s.verify = func(*instance, Route) {}
	return s
}

func (s *server) name() string {
	if s.relay {
		return "relay"
	}
	return "dialer"
}

// relayRoutes keeps the routes that name a relay socket, pointed at their Listen address.
func relayRoutes(routes Routes) Routes {
	relayed := Routes{Instances: map[string]Route{}}
	for name, route := range routes.Instances {
		if route.Relay != "" {
			relayed.Instances[name] = Route{Listen: route.Relay, Targets: []string{route.Listen}, Revision: route.Revision}
		}
	}
	return relayed
}

func (s *server) run() {
	log.Printf("%s %s serving %s", s.name(), version, s.dir)
	started, idle := time.Now(), time.Now()
	for tick := 0; ; tick++ {
		if tick%10 == 0 && !s.relay {
			s.refreshInterface(true)
		}
		routes, err := readRoutes(filepath.Join(s.dir, "routes.json"))
		if err != nil {
			log.Printf("routes: %v", err)
		}
		if s.relay {
			routes = relayRoutes(routes)
		}
		s.reconcile(routes)
		s.writeStatus()
		if len(routes.Instances) > 0 {
			idle = time.Now()
		} else if time.Since(idle) > idleExit && time.Since(started) > startGrace {
			log.Printf("no routes; exiting")
			return
		}
		time.Sleep(time.Second)
	}
}

func readRoutes(path string) (Routes, error) {
	var routes Routes
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return routes, nil
	}
	if err != nil {
		return routes, err
	}
	if err := json.Unmarshal(data, &routes); err != nil {
		return Routes{}, err
	}
	for name, route := range routes.Instances {
		if validRoute(route) != nil {
			log.Printf("ignoring invalid route for %s: %v", name, validRoute(route))
			delete(routes.Instances, name)
		}
	}
	return routes, nil
}

// validRoute accepts only literal public-looking IP targets and local listeners.
func validRoute(route Route) error {
	if len(route.Targets) == 0 || len(route.Targets) > 4 {
		return errors.New("1-4 targets required")
	}
	for _, target := range route.Targets {
		host, port, err := net.SplitHostPort(target)
		if err != nil || net.ParseIP(host) == nil || port == "" || port == "0" {
			return fmt.Errorf("invalid target %q", target)
		}
	}
	if route.Relay != "" {
		if path, ok := strings.CutPrefix(route.Relay, "unix:"); !ok || !filepath.IsAbs(path) {
			return errors.New("relay must be an absolute unix: path")
		}
	}
	if path, ok := strings.CutPrefix(route.Listen, "unix:"); ok {
		if !filepath.IsAbs(path) {
			return errors.New("unix listener needs an absolute path")
		}
		return nil
	}
	host, _, err := net.SplitHostPort(route.Listen)
	if err != nil || host != "127.0.0.1" {
		return errors.New("tcp listener must be on 127.0.0.1")
	}
	return nil
}

func (s *server) reconcile(routes Routes) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for name, current := range s.instances {
		route, ok := routes.Instances[name]
		if !ok || route.Listen != current.route.Load().Listen {
			current.close()
			delete(s.instances, name)
		}
	}
	for name, route := range routes.Instances {
		route := route
		current, ok := s.instances[name]
		if !ok {
			current = &instance{name: name, status: &InstanceStatus{Listen: route.Listen}}
			current.route.Store(&route)
			if err := current.listen(s); err != nil {
				current.status.ListenError = err.Error()
				log.Printf("%s: listen %s: %v", name, route.Listen, err)
			}
			s.instances[name] = current
		}
		previous := current.route.Load()
		current.route.Store(&route)
		if !ok || previous.Revision != route.Revision {
			current.status.Revision = route.Revision
			go s.verify(current, route)
		}
	}
}

func (i *instance) listen(s *server) error {
	route := i.route.Load()
	network, address := "tcp", route.Listen
	if path, ok := strings.CutPrefix(route.Listen, "unix:"); ok {
		network, address = "unix", path
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		_ = os.Remove(path)
	}
	listener, err := net.Listen(network, address)
	if err != nil {
		return err
	}
	i.listener = listener
	i.status.Listening = true
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go s.forward(i, conn)
		}
	}()
	return nil
}

func (i *instance) close() {
	if i.listener != nil {
		_ = i.listener.Close()
	}
}

// refreshInterface re-selects the physical interface, at most every 5 seconds
// unless forced (network changes such as Wi-Fi to Ethernet are picked up).
func (s *server) refreshInterface(force bool) (Iface, error) {
	s.mu.Lock()
	iface, err, fresh := s.iface, s.ifaceErr, !force && time.Since(s.ifaceAt) <= 5*time.Second
	s.mu.Unlock()
	if fresh {
		return iface, err
	}
	// Route lookups may run external commands; never hold the lock for them.
	iface, err = physicalInterface()
	sample := ""
	if force {
		sample = routeInterface(sampleIP)
	}
	s.mu.Lock()
	s.iface, s.ifaceErr, s.ifaceAt = iface, err, time.Now()
	if force {
		s.sample = sample
		s.tunnel = err == nil && sample != "" && sample != iface.Name
	}
	s.mu.Unlock()
	return iface, err
}

func boundDialer(iface Iface) *net.Dialer {
	return &net.Dialer{Timeout: dialTimeout, Control: func(network, address string, raw syscall.RawConn) error {
		var bindErr error
		if err := raw.Control(func(fd uintptr) { bindErr = bindSocket(fd, iface) }); err != nil {
			return err
		}
		return bindErr
	}}
}

func (s *server) forward(i *instance, client net.Conn) {
	defer client.Close()
	route := i.route.Load()
	var iface Iface
	var err error
	if !s.relay {
		iface, err = s.refreshInterface(false)
	}
	var upstream net.Conn
	if err == nil {
		for _, target := range route.Targets {
			upstream, err = s.dial(iface, target)
			if err == nil {
				break
			}
		}
	}
	s.mu.Lock()
	if err != nil {
		i.status.Failures++
		i.status.LastError = describe(err)
	} else {
		i.status.Connections++
	}
	s.mu.Unlock()
	if err != nil {
		return
	}
	defer upstream.Close()
	pipe(client, upstream)
}

type halfCloser interface{ CloseWrite() error }

func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	copyHalf := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if closer, ok := dst.(halfCloser); ok {
			_ = closer.CloseWrite()
		} else {
			_ = dst.Close()
		}
		done <- struct{}{}
	}
	go copyHalf(a, b)
	go copyHalf(b, a)
	<-done
	<-done
}

// check proves the bound path reaches the node's TLS endpoint, and records how
// the host's default path compares (a different exit means a TUN was bypassed).
func (s *server) check(i *instance, route Route) {
	iface, err := s.refreshInterface(true)
	result := &Check{Revision: route.Revision, Interface: iface.Name}
	if err != nil {
		result.Bound = Probe{Error: "no physical interface: " + err.Error()}
	} else {
		bound := boundDialer(iface)
		result.Bound = probeTLS(bound, route)
		result.BoundExit = exitIP(bound)
	}
	plain := &net.Dialer{Timeout: dialTimeout}
	result.Default = probeTLS(plain, route)
	result.DefaultExit = exitIP(plain)
	s.mu.Lock()
	if i.status.Revision == route.Revision {
		i.status.Check = result
	}
	s.mu.Unlock()
	log.Printf("%s: check bound=%v default=%v via %s", i.name, result.Bound.OK, result.Default.OK, iface.Name)
}

func probeTLS(dialer *net.Dialer, route Route) Probe {
	start := time.Now()
	var last error
	for _, target := range route.Targets {
		conn, err := dialer.Dial("tcp", target)
		if err != nil {
			last = err
			continue
		}
		name := route.SNI
		if net.ParseIP(name) != nil {
			name = ""
		}
		// Only reachability of the node's TLS endpoint is tested; no data is sent.
		client := tls.Client(conn, &tls.Config{ServerName: name, InsecureSkipVerify: true}) //nolint:gosec
		ctx, cancel := context.WithTimeout(context.Background(), dialTimeout)
		err = client.HandshakeContext(ctx)
		cancel()
		_ = client.Close()
		if err == nil {
			return Probe{OK: true, Ms: time.Since(start).Milliseconds()}
		}
		last = err
	}
	return Probe{Ms: time.Since(start).Milliseconds(), Error: describe(last)}
}

func exitIP(dialer *net.Dialer) string {
	client := &http.Client{Timeout: 8 * time.Second, Transport: &http.Transport{
		Proxy: nil, DialContext: dialer.DialContext, TLSHandshakeTimeout: dialTimeout}}
	response, err := client.Get(traceURL)
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
	return traceField(string(body), "ip")
}

func traceField(body, key string) string {
	for _, line := range strings.Split(body, "\n") {
		if value, ok := strings.CutPrefix(strings.TrimSpace(line), key+"="); ok {
			return value
		}
	}
	return ""
}

func describe(err error) string {
	if err == nil {
		return ""
	}
	var netErr net.Error
	switch {
	case errors.As(err, &netErr) && netErr.Timeout():
		return "timed out"
	case errors.Is(err, io.EOF), strings.Contains(err.Error(), "EOF"):
		return "connection closed during the TLS handshake"
	case strings.Contains(err.Error(), "refused"):
		return "connection refused"
	}
	return "connection failed"
}

func (s *server) writeStatus() {
	s.mu.Lock()
	status := Status{Version: version, PID: os.Getpid(), Heartbeat: time.Now().Unix(), Interface: s.iface.Name,
		SampleRoute: s.sample, Tunnel: s.tunnel, Instances: map[string]*InstanceStatus{}}
	if s.ifaceErr != nil {
		status.InterfaceError = s.ifaceErr.Error()
	}
	for name, current := range s.instances {
		copied := *current.status
		status.Instances[name] = &copied
	}
	data, err := json.MarshalIndent(status, "", "  ")
	s.mu.Unlock()
	if err != nil {
		return
	}
	path := filepath.Join(s.dir, "status.json")
	if s.relay {
		path = filepath.Join(s.dir, "relay-status.json")
	}
	temporary := path + ".tmp"
	if os.WriteFile(temporary, data, 0o600) == nil {
		_ = os.Rename(temporary, path)
	}
}
