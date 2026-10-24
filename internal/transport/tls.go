package transport

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
)

func TLS(dir string, server bool) (*tls.Config, error) {
	ca, err := os.ReadFile(dir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("invalid CA bundle")
	}
	cert, err := tls.LoadX509KeyPair(dir+"/tls.crt", dir+"/tls.key")
	if err != nil {
		return nil, err
	}
	c := &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}, RootCAs: roots}
	if server {
		c.ClientCAs = roots
		c.ClientAuth = tls.VerifyClientCertIfGiven
	}
	return c, nil
}
