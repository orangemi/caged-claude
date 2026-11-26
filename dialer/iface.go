package main

import (
	"errors"
	"net"
	"regexp"
)

// Interfaces that carry a host's own traffic without a TUN or VPN in between.
var physicalNames = map[string]*regexp.Regexp{
	"darwin": regexp.MustCompile(`^en\d+$`),
	"linux":  regexp.MustCompile(`^(?:(?:en|eth|wl|ww|em|bond)\w*|br\d+)$`),
}

var errNoInterface = errors.New("no physical interface with an IPv4 address is up")

// firstWithIPv4 picks the first up interface whose name looks physical.
func firstWithIPv4(physical *regexp.Regexp, preferred string) (Iface, error) {
	interfaces, err := net.Interfaces()
	if err != nil {
		return Iface{}, err
	}
	var found *Iface
	for _, candidate := range interfaces {
		if candidate.Flags&net.FlagUp == 0 || !physical.MatchString(candidate.Name) || !hasIPv4(candidate) {
			continue
		}
		if candidate.Name == preferred {
			return Iface{Name: candidate.Name, Index: candidate.Index}, nil
		}
		if found == nil {
			found = &Iface{Name: candidate.Name, Index: candidate.Index}
		}
	}
	if found == nil {
		return Iface{}, errNoInterface
	}
	return *found, nil
}

func hasIPv4(candidate net.Interface) bool {
	addresses, err := candidate.Addrs()
	if err != nil {
		return false
	}
	for _, address := range addresses {
		if network, ok := address.(*net.IPNet); ok && network.IP.To4() != nil && !network.IP.IsLoopback() && !network.IP.IsLinkLocalUnicast() {
			return true
		}
	}
	return false
}
