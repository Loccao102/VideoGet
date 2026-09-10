package discovery

import (
	"sort"
	"strings"
)

// Expand returns a small set of high-signal search phrases for Chinese short-video platforms.
// It intentionally stays deterministic so discovery still works without an LLM/API.
func Expand(keyword string) []string {
	keyword = strings.TrimSpace(keyword)
	if keyword == "" {
		return nil
	}

	out := []string{keyword}
	lower := strings.ToLower(keyword)

	groups := []struct {
		match []string
		terms []string
	}{
		{[]string{"đồ bếp", "nha bep", "nhà bếp", "kitchen"}, []string{"厨房好物", "厨房神器", "懒人厨房", "备餐神器"}},
		{[]string{"tẩy rửa", "ve sinh", "vệ sinh", "cleaning", "làm sạch"}, []string{"清洁好物", "厨房清洁神器", "去污神器", "去油污"}},
		{[]string{"đồ gia dụng", "gia dung", "home", "life hack", "tiện ích"}, []string{"居家好物", "生活好物", "实用好物", "提升幸福感好物"}},
		{[]string{"meal prep", "bảo quản", "bao quan", "đông lạnh", "dong lanh"}, []string{"备餐", "冻门", "保鲜神器", "保鲜盒", "真空袋"}},
		{[]string{"cà phê", "ca phe", "coffee"}, []string{"户外咖啡", "便携咖啡", "手冲咖啡", "咖啡好物"}},
		{[]string{"camping", "cắm trại", "cam trai", "outdoor"}, []string{"露营好物", "户外好物", "户外装备", "便携好物"}},
		{[]string{"pet", "thú cưng", "thu cung", "mèo", "meo", "chó", "cho"}, []string{"宠物好物", "养猫好物", "养狗好物", "宠物用品"}},
		{[]string{"skincare", "chăm sóc cá nhân", "cham soc ca nhan", "personal care"}, []string{"个人护理好物", "洗护好物", "身体护理", "好物测评"}},
		{[]string{"ai agent", "agent ai"}, []string{"AI智能体", "智能体", "人工智能Agent"}},
		{[]string{"microservice", "micro service", "vi dịch vụ"}, []string{"微服务", "微服务架构", "分布式系统", "服务拆分"}},
	}

	for _, group := range groups {
		matched := false
		for _, marker := range group.match {
			if strings.Contains(lower, marker) {
				matched = true
				break
			}
		}
		if matched {
			out = append(out, group.terms...)
		}
	}

	seen := map[string]struct{}{}
	unique := make([]string, 0, len(out))
	for _, value := range out {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		key := strings.ToLower(value)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		unique = append(unique, value)
	}

	// Keep the original keyword first; sort the generated terms for stable API responses/tests.
	if len(unique) > 2 {
		sort.Strings(unique[1:])
	}
	if len(unique) > 6 {
		unique = unique[:6]
	}
	return unique
}
