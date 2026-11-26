package main

import (
	"os/exec"
	"regexp"
	"syscall"
)

var routeInterfaceLine = regexp.MustCompile(`(?m)^\s*interface:\s*(\S+)`)

// A TUN usually installs more specific routes (1.0.0.0/8, ...) and leaves the
// default route on the physical interface, so the default route is preferred.
func physicalInterface() (Iface, error) {
	return firstWithIPv4(physicalNames["darwin"], routeInterface("default"))
}

func routeInterface(destination string) string {
	output, err := exec.Command("/sbin/route", "-n", "get", destination).Output()
	if err != nil {
		return ""
	}
	if match := routeInterfaceLine.FindSubmatch(output); match != nil {
		return string(match[1])
	}
	return ""
}

// IP_BOUND_IF scopes the socket to the interface, ignoring the routes a TUN added.
func bindSocket(fd uintptr, iface Iface) error {
	return syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_BOUND_IF, iface.Index)
}
