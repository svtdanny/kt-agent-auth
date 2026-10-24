package controlclient

import (
	"bytes"
	"context"
	"encoding/json"
	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/transport"
	"io"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	Base string
	HTTP *http.Client
}

func New(base, dir string) (*Client, error) {
	t, err := transport.TLS(dir, false)
	if err != nil {
		return nil, err
	}
	return &Client{Base: base, HTTP: &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: t}}}, nil
}
func (c *Client) call(ctx context.Context, path string, input, output any) error {
	method := http.MethodGet
	var body io.Reader
	if input != nil {
		raw, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
		method = http.MethodPost
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		var body struct {
			Error string `json:"error"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&body)
		return &api.ControlError{Status: resp.StatusCode, Message: body.Error}
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(output)
}
func (c *Client) Resolve(ctx context.Context, id string) (api.SessionContext, error) {
	var out api.SessionContext
	err := c.call(ctx, "/internal/context?spiffeId="+url.QueryEscape(id), nil, &out)
	return out, err
}
func (c *Client) Credential(ctx context.Context, r api.CredentialRequest) (map[string]string, error) {
	var out map[string]string
	err := c.call(ctx, "/internal/credential", r, &out)
	return out, err
}
