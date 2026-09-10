package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/download"
	"github.com/Loccao102/VideoGet/internal/model"
	"github.com/Loccao102/VideoGet/internal/source"
)

type Server struct{ providers map[string]source.Provider; jobs *download.Manager; web fs.FS }
func New(providers []source.Provider,jobs *download.Manager,web fs.FS)*Server{m:=map[string]source.Provider{};for _,p:=range providers{m[p.Name()]=p};return &Server{providers:m,jobs:jobs,web:web}}

func (s *Server) Handler() http.Handler { mux:=http.NewServeMux(); mux.HandleFunc("GET /api/health",s.health);mux.HandleFunc("POST /api/search",s.search);mux.HandleFunc("POST /api/download",s.download);mux.HandleFunc("GET /api/jobs",s.listJobs);mux.HandleFunc("GET /api/jobs/{id}",s.getJob); if s.web!=nil{mux.Handle("/",http.FileServer(http.FS(s.web)))}; return requestLogger(mux) }
func (s *Server) health(w http.ResponseWriter,_ *http.Request){p:=map[string]map[string]any{};for name,provider:=range s.providers{err:=provider.Available();p[name]=map[string]any{"available":err==nil};if err!=nil{p[name]["error"]=err.Error()}};writeJSON(w,200,map[string]any{"status":"ok","providers":p,"time":time.Now().UTC()})}
func (s *Server) search(w http.ResponseWriter,r *http.Request){var req model.SearchRequest;if err:=json.NewDecoder(http.MaxBytesReader(w,r.Body,1<<20)).Decode(&req);err!=nil{writeError(w,400,"invalid JSON body");return};req.Keyword=strings.TrimSpace(req.Keyword);if req.Keyword==""{writeError(w,400,"keyword is required");return};if req.Limit<=0{req.Limit=20};if req.Limit>100{req.Limit=100};if len(req.Sources)==0{for n:=range s.providers{req.Sources=append(req.Sources,n)};sort.Strings(req.Sources)};type pr struct{name string; videos []model.Video; err error};ch:=make(chan pr,len(req.Sources));var wg sync.WaitGroup;seen:=map[string]struct{}{};for _,raw:=range req.Sources{name:=strings.ToLower(strings.TrimSpace(raw));if _,ok:=seen[name];ok{continue};seen[name]=struct{}{};provider,ok:=s.providers[name];if !ok{ch<-pr{name:name,err:fmt.Errorf("unknown source")};continue};wg.Add(1);go func(name string,p source.Provider){defer wg.Done();ctx,cancel:=context.WithTimeout(r.Context(),60*time.Second);defer cancel();videos,err:=p.Search(ctx,req.Keyword,req.Limit);ch<-pr{name:name,videos:videos,err:err}}(name,provider)};go func(){wg.Wait();close(ch)}();resp:=model.SearchResponse{Keyword:req.Keyword,Results:[]model.Video{},Errors:map[string]string{}};for x:=range ch{if x.err!=nil{resp.Errors[x.name]=x.err.Error()}else{resp.Results=append(resp.Results,x.videos...)}};if len(resp.Errors)==0{resp.Errors=nil};writeJSON(w,200,resp)}
func (s *Server) download(w http.ResponseWriter,r *http.Request){var req struct{Video model.Video `json:"video"`};if err:=json.NewDecoder(http.MaxBytesReader(w,r.Body,2<<20)).Decode(&req);err!=nil{writeError(w,400,"invalid JSON body");return};job,err:=s.jobs.Start(req.Video);if err!=nil{writeError(w,400,err.Error());return};writeJSON(w,http.StatusAccepted,job)}
func (s *Server) getJob(w http.ResponseWriter,r *http.Request){job,ok:=s.jobs.Get(r.PathValue("id"));if !ok{writeError(w,404,"job not found");return};writeJSON(w,200,job)}
func (s *Server) listJobs(w http.ResponseWriter,_ *http.Request){writeJSON(w,200,s.jobs.List())}
func writeJSON(w http.ResponseWriter,status int,value any){w.Header().Set("Content-Type","application/json; charset=utf-8");w.Header().Set("Cache-Control","no-store");w.WriteHeader(status);_ = json.NewEncoder(w).Encode(value)}
func writeError(w http.ResponseWriter,status int,msg string){writeJSON(w,status,map[string]string{"error":msg})}
func requestLogger(next http.Handler)http.Handler{return http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){start:=time.Now();next.ServeHTTP(w,r);log.Printf("%s %s %s",r.Method,r.URL.Path,time.Since(start).Round(time.Millisecond))})}
