package main

import (
	"encoding/binary"
	"errors"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

const ipUnicastIf = 31 // IP_UNICAST_IF

func powershell(script string) (string, error) {
	output, err := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script).Output()
	return strings.TrimSpace(string(output)), err
}

// A hardware adapter (Get-NetAdapter -Physical) that is up and holds a default
// route; TUN adapters such as wintun are never physical.
func physicalInterface() (Iface, error) {
	output, err := powershell(`Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | ForEach-Object {
  $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -InterfaceIndex $_.ifIndex -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
  if ($route) { '{0} {1} {2}' -f ($route.RouteMetric + $_.InterfaceMetric), $_.ifIndex, $_.Name }
} | Sort-Object { [int]($_ -split ' ')[0] } | Select-Object -First 1`)
	if err != nil {
		return Iface{}, err
	}
	fields := strings.SplitN(output, " ", 3)
	if len(fields) != 3 {
		return Iface{}, errors.New("no physical adapter with a default route is up")
	}
	index, err := strconv.Atoi(fields[1])
	if err != nil {
		return Iface{}, err
	}
	return Iface{Name: fields[2], Index: index}, nil
}

func routeInterface(destination string) string {
	output, _ := powershell("(Find-NetRoute -RemoteIPAddress '" + destination + "' | Select-Object -First 1).InterfaceAlias")
	return output
}

// IP_UNICAST_IF takes the interface index in network byte order for IPv4.
func bindSocket(fd uintptr, iface Iface) error {
	var value [4]byte
	binary.BigEndian.PutUint32(value[:], uint32(iface.Index))
	return syscall.SetsockoptInt(syscall.Handle(fd), syscall.IPPROTO_IP, ipUnicastIf, int(binary.LittleEndian.Uint32(value[:])))
}
