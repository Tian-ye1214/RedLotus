---
name: web-scraping
description: 网页数据抓取和解析。用于从网站提取结构化数据、自动化网页操作、处理动态页面内容。当需要从网页获取数据时使用此Skill。
---

# 网页抓取 Skill

本 Skill 提供网页数据抓取和解析能力，支持静态页面和动态渲染页面。

## 快速开始

### 静态页面抓取

使用 `requests` 和 `BeautifulSoup` 处理静态页面：

```python
import requests
from bs4 import BeautifulSoup

def scrape_static_page(url: str) -> dict:
    """抓取静态网页内容"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    return {
        'title': soup.title.string if soup.title else None,
        'text': soup.get_text(separator='\n', strip=True),
        'links': [a.get('href') for a in soup.find_all('a', href=True)],
        'images': [img.get('src') for img in soup.find_all('img', src=True)]
    }
```

### 提取特定元素

```python
def extract_elements(html: str, selector: str) -> list:
    """使用 CSS 选择器提取元素"""
    soup = BeautifulSoup(html, 'html.parser')
    elements = soup.select(selector)
    return [elem.get_text(strip=True) for elem in elements]
```

## 工作流程

1. **分析目标页面**: 确定页面是静态还是动态渲染
2. **选择抓取方式**:
   - 静态页面 → requests + BeautifulSoup
   - 动态页面 → Selenium 或 Playwright
3. **定位目标数据**: 使用 CSS 选择器或 XPath
4. **提取并清洗数据**: 去除无用内容，格式化输出
5. **保存结果**: 保存为 JSON、CSV 或其他格式

## 最佳实践

- 设置合理的请求间隔，避免对目标服务器造成压力
- 处理请求失败的情况，实现重试机制
- 尊重 robots.txt 规则
- 注意处理编码问题

## 常见选择器

| 选择器 | 说明 |
|--------|------|
| `#id` | 选择指定 ID 的元素 |
| `.class` | 选择指定类名的元素 |
| `tag` | 选择指定标签的元素 |
| `[attr=value]` | 选择指定属性值的元素 |

