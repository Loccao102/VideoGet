package download

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type JobStatus string
const ( JobQueued JobStatus="queued"; JobDownloading JobStatus="downloading"; JobDone JobStatus="done"; JobFailed JobStatus="failed" )

type Job struct { ID string `json:"id"`; Status JobStatus `json:"status"`; Video model.Video `json:"video"`; Output string `json:"output,omitempty"`; Error string `json:"error,omitempty"`; CreatedAt time.Time `json:"createdAt"`; UpdatedAt time.Time `json:"updatedAt"` }
type Manager struct { mu sync.RWMutex; jobs map[string]Job; downloadDir string }

func NewManager(downloadDir string)*Manager{ if strings.TrimSpace(downloadDir)==""{downloadDir="downloads"}; return &Manager{jobs:map[string]Job{},downloadDir:downloadDir} }
func (m *Manager) Start(video model.Video)(Job,error){ if video.URL==""||video.Platform==""{return Job{},fmt.Errorf("platform and url are required")}; if err:=os.MkdirAll(m.downloadDir,0755);err!=nil{return Job{},err}; now:=time.Now().UTC(); job:=Job{ID:newID(),Status:JobQueued,Video:video,CreatedAt:now,UpdatedAt:now}; m.mu.Lock();m.jobs[job.ID]=job;m.mu.Unlock(); go m.run(job.ID); return job,nil }
func (m *Manager) Get(id string)(Job,bool){m.mu.RLock();defer m.mu.RUnlock();j,ok:=m.jobs[id];return j,ok}
func (m *Manager) List()[]Job{m.mu.RLock();defer m.mu.RUnlock();out:=make([]Job,0,len(m.jobs));for _,j:=range m.jobs{out=append(out,j)};return out}
func (m *Manager) update(id string,fn func(*Job)){m.mu.Lock();defer m.mu.Unlock();j,ok:=m.jobs[id];if !ok{return};fn(&j);m.jobs[id]=j}
func (m *Manager) run(id string){job,ok:=m.Get(id);if !ok{return};m.update(id,func(j *Job){j.Status=JobDownloading;j.UpdatedAt=time.Now().UTC()});ctx,cancel:=context.WithTimeout(context.Background(),45*time.Minute);defer cancel();out,err:=m.download(ctx,job.Video);m.update(id,func(j *Job){j.UpdatedAt=time.Now().UTC();if err!=nil{j.Status=JobFailed;j.Error=err.Error()}else{j.Status=JobDone;j.Output=out}})}
func (m *Manager) download(ctx context.Context,v model.Video)(string,error){switch strings.ToLower(v.Platform){case "bilibili":return m.ytdlp(ctx,v.URL);case "douyin":return m.douyin(ctx,v.URL);default:return "",fmt.Errorf("unsupported platform %q",v.Platform)}}
func (m *Manager) ytdlp(ctx context.Context,url string)(string,error){bin,err:=exec.LookPath("yt-dlp");if err!=nil{return "",fmt.Errorf("yt-dlp is not installed: %w",err)};tpl:=filepath.Join(m.downloadDir,"%(title).120B [%(id)s].%(ext)s");cmd:=exec.CommandContext(ctx,bin,"--ignore-config","--no-playlist","--no-progress","--merge-output-format","mp4","--print","after_move:filepath","-o",tpl,url);var stdout,stderr bytes.Buffer;cmd.Stdout=&stdout;cmd.Stderr=&stderr;if err:=cmd.Run();err!=nil{return "",fmt.Errorf("yt-dlp download failed: %s",strings.TrimSpace(stderr.String()))};lines:=strings.Fields(stdout.String());if len(lines)>0{return lines[len(lines)-1],nil};return m.downloadDir,nil}
func (m *Manager) douyin(ctx context.Context,url string)(string,error){binary:=strings.TrimSpace(os.Getenv("DOUYIN_BIN"));if binary==""{binary="douyin"};bin,err:=exec.LookPath(binary);if err!=nil{return "",fmt.Errorf("douyin-cli is not installed: %w",err)};cmd:=exec.CommandContext(ctx,bin,"-u",url,"-t","aweme","-p",m.downloadDir);cmd.Env=os.Environ();out,err:=cmd.CombinedOutput();if err!=nil{return "",fmt.Errorf("douyin download failed: %s",strings.TrimSpace(string(out)))};return m.downloadDir,nil}
func newID()string{b:=make([]byte,8);if _,err:=rand.Read(b);err==nil{return hex.EncodeToString(b)};return fmt.Sprintf("%d",time.Now().UnixNano())}
