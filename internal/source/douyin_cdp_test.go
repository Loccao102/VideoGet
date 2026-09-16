package source

import "testing"

func TestParseDouyinSearchAPIBodies(t *testing.T) {
	body := `{
		"status_code":0,
		"data":[
			{"type":1,"aweme_info":{
				"aweme_id":"7654321098765432101",
				"desc":"实用数码产品测评",
				"create_time":1789000000,
				"author":{"nickname":"数码实验室"},
				"statistics":{"play_count":120000,"digg_count":9300,"comment_count":420,"share_count":188},
				"video":{"duration":42000,"cover":{"url_list":["https://example.com/cover.jpg"]}}
			}},
			{"type":1,"aweme_info":{"aweme_id":"7654321098765432102","desc":"黑科技好物"}}
		]
	}`

	results, err := parseDouyinSearchAPIBodies([]string{body}, "实用数码产品", 10)
	if err != nil {
		t.Fatalf("parseDouyinSearchAPIBodies error: %v", err)
	}
	if len(results) != 2 {
		t.Fatalf("len(results) = %d, want 2", len(results))
	}
	first := results[0]
	if first.ID != "7654321098765432101" {
		t.Fatalf("first ID = %q", first.ID)
	}
	if first.Title != "实用数码产品测评" || first.Author != "数码实验室" {
		t.Fatalf("unexpected metadata: title=%q author=%q", first.Title, first.Author)
	}
	if first.Views != 120000 || first.Likes != 9300 || first.Comments != 420 || first.Shares != 188 {
		t.Fatalf("unexpected stats: %+v", first)
	}
	if first.DurationSec != 42 {
		t.Fatalf("DurationSec = %d, want 42", first.DurationSec)
	}
	if first.Thumbnail != "https://example.com/cover.jpg" {
		t.Fatalf("Thumbnail = %q", first.Thumbnail)
	}
}

func TestParseDouyinSearchAPIBodiesRequiresLogin(t *testing.T) {
	body := `{"status_code":2483,"status_msg":"请先登录，再继续搜索吧","data":[]}`
	results, err := parseDouyinSearchAPIBodies([]string{body}, "黑科技好物", 10)
	if len(results) != 0 {
		t.Fatalf("len(results) = %d, want 0", len(results))
	}
	if err == nil {
		t.Fatal("expected login-required error")
	}
}

func TestParseDouyinSearchAPIBodiesDeduplicatesPages(t *testing.T) {
	page1 := `{"status_code":0,"data":[{"type":1,"aweme_info":{"aweme_id":"7654321098765432101","desc":"A"}}]}`
	page2 := `{"status_code":0,"data":[{"type":1,"aweme_info":{"aweme_id":"7654321098765432101","desc":"A duplicate"}},{"type":1,"aweme_info":{"aweme_id":"7654321098765432102","desc":"B"}}]}`
	results, err := parseDouyinSearchAPIBodies([]string{page1, page2}, "数码", 10)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(results) != 2 {
		t.Fatalf("len(results) = %d, want 2", len(results))
	}
}
