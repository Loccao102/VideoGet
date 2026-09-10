package source

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type DouyinProvider struct{ binary string }

func NewDouyinProvider() *DouyinProvider {
	binary := strings.TrimSpace(os.Getenv("DOUYIN_BIN")); if binary == "" { binary = "douyin" }
	return &DouyinProvider{binary: binary}
}
func (p *DouyinProvider) Name() string { return "douyin" }
func (p *DouyinProvider) Available() error { _, err := executable(p.binary); return err }

func (p *DouyinProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	bin, err := executable(p.binary); if err != nil { return nil, err }
	tmp, err := os.MkdirTemp("", "videoget-douyin-*"); if err != nil { return nil, fmt.Errorf("create temp dir: %w", err) }; defer os.RemoveAll(tmp)
	cmd := exec.CommandContext(ctx, bin, "-u", keyword, "-t", "search", "-l", strconv.Itoa(limit), "--no-download", "-p", tmp); cmd.Env = os.Environ()
	output, err := cmd.CombinedOutput(); if err != nil { return nil, fmt.Errorf("douyin search failed: %s", strings.TrimSpace(string(output))) }
	files, _ := filepath.Glob(filepath.Join(tmp, "*.json")); if len(files)==0 { return nil, fmt.Errorf("douyin-cli completed but no metadata JSON was produced") }; sort.Strings(files)
	data, err := os.ReadFile(files[len(files)-1]); if err != nil { return nil, err }
	var items []map[string]any; if err := json.Unmarshal(data, &items); err != nil { return nil, fmt.Errorf("parse douyin metadata: %w", err) }
	results := make([]model.Video,0,len(items))
	for _, item := range items {
		id:=asString(item["id"]); if id=="" { continue }
		duration:=asInt64(item["duration"]); if duration>10000 { duration/=1000 }
		v:=model.Video{ID:id, Platform:p.Name(), Title:asString(item["desc"]), Author:asString(item["author_nickname"]), URL:"https://www.douyin.com/video/"+id, Thumbnail:asString(item["cover"]), DurationSec:duration, Likes:asInt64(item["digg_count"]), Comments:asInt64(item["comment_count"]), Shares:asInt64(item["share_count"]), DownloadURL:asString(item["download_addr"]), SearchSource:keyword}
		if ts:=asInt64(item["time"]); ts>0 { t:=time.Unix(ts,0).UTC(); v.PublishedAt=&t }
		results=append(results,v)
	}
	return results,nil
}

func asString(value any) string { switch v:=value.(type) { case string: return v; case json.Number: return v.String(); case float64: return strconv.FormatInt(int64(v),10); default: return "" } }
func asInt64(value any) int64 { switch v:=value.(type) { case float64: return int64(v); case int64:return v; case int:return int64(v); case json.Number:n,_:=v.Int64();return n; case string:n,_:=strconv.ParseInt(v,10,64);return n; default:return 0 } }
