package main

import (
	"bufio"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"syscall"
)

var routeDevice = regexp.MustCompile(`\bdev (\S+)`)

// The main table's default route stays on the physical interface while a TUN
// captures traffic through policy-routing rules; the lowest metric wins.
func physicalInterface() (Iface, error) {
	return firstWithIPv4(physicalNames["linux"], defaultRouteDevice("/proc/net/route"))
}

func defaultRouteDevice(path string) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	best, bestMetric := "", -1
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 7 || fields[1] != "00000000" || !physicalNames["linux"].MatchString(fields[0]) {
			continue
		}
		metric, err := strconv.Atoi(fields[6])
		if err == nil && (bestMetric < 0 || metric < bestMetric) {
			best, bestMetric = fields[0], metric
		}
	}
	return best
}

func routeInterface(destination string) string {
	output, err := exec.Command("ip", "route", "get", destination).Output()
	if err != nil {
		return ""
	}
	if match := routeDevice.FindSubmatch(output); match != nil {
		return string(match[1])
	}
	return ""
}

// SO_BINDTODEVICE needs no privilege since Linux 5.7.
func bindSocket(fd uintptr, iface Iface) error {
	return syscall.BindToDevice(int(fd), iface.Name)
}
