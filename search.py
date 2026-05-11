"""
联网搜索模块 - 支持 Exa AI 和 Tavily 两种搜索引擎
异步实现，可作为库被其他异步模块引用
"""
import os
import asyncio
import aiohttp
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# 仅在直接执行时加载环境变量，作为库引用时由主程序负责
if __name__ == "__main__":
    load_dotenv()


class SearchService:
    """搜索服务类，支持 Exa AI 和 Tavily（异步版本）"""

    def __init__(self,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 verbose: bool = False,
                 session: Optional[aiohttp.ClientSession] = None,
                 engine: Optional[str] = None):
        """
        初始化异步搜索服务

        Args:
            api_key: API密钥，默认从环境变量读取
            base_url: API基础URL，默认从环境变量读取或使用官方地址
            verbose: 是否输出详细日志，默认False
            session: 可选的 aiohttp.ClientSession，如果不提供则自动创建
            engine: 搜索引擎，'exa' 或 'tavily'，默认从环境变量读取
        """
        load_dotenv()
        
        # 确定搜索引擎
        self.engine = engine or os.getenv('SEARCH_ENGINE', 'tavily').lower()
        
        if self.engine == 'tavily':
            self.api_key = api_key or os.getenv('TAVILY_API_KEY')
            self.base_url = base_url or os.getenv('TAVILY_BASE_URL', 'https://api.tavily.com')
            if not self.api_key:
                raise ValueError("TAVILY_API_KEY 未配置。请通过参数传入或设置环境变量。")
            self.headers = {
                'Content-Type': 'application/json',
            }
        else:  # exa
            self.api_key = api_key or os.getenv('EXA_API_KEY')
            self.base_url = base_url or os.getenv('EXA_BASE_URL', 'https://api.exa.ai')
            if not self.api_key:
                raise ValueError("EXA_API_KEY 未配置。请通过参数传入或设置环境变量。")
            self.headers = {
                'Content-Type': 'application/json',
                'x-api-key': self.api_key,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }

        self.verbose = verbose
        self._external_session = session  # 外部提供的 session
        self._internal_session = None     # 内部创建的 session

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp session"""
        if self._external_session:
            return self._external_session

        if self._internal_session is None or self._internal_session.closed:
            self._internal_session = aiohttp.ClientSession()

        return self._internal_session

    async def close(self):
        """关闭内部创建的 session（如果存在）"""
        if self._internal_session and not self._internal_session.closed:
            await self._internal_session.close()
            self._internal_session = None

    async def _search_tavily(self,
                              query: str,
                              num_results: int = 3,
                              search_depth: str = "basic",
                              timeout: int = 30) -> Dict[str, Any]:
        """使用 Tavily 搜索"""
        url = f"{self.base_url}/search"

        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": num_results,
            "include_answer": True,
            "include_raw_content": False
        }

        if self.verbose:
            print(f"[SearchService-Tavily] 正在搜索: {query}")

        session = await self._get_session()
        timeout_config = aiohttp.ClientTimeout(total=timeout)

        try:
            async with session.post(url, json=payload, timeout=timeout_config) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

                return await response.json()

        except asyncio.TimeoutError:
            raise Exception(f"搜索请求超时（超过 {timeout} 秒）")
        except aiohttp.ClientError as e:
            raise Exception(f"网络请求失败: {str(e)}")

    async def _search_exa(self,
                           query: str,
                           num_results: int = 3,
                           use_autoprompt: bool = True,
                           include_text: bool = False,
                           include_highlights: bool = True,
                           timeout: int = 30) -> Dict[str, Any]:
        """使用 Exa AI 搜索"""
        url = f"{self.base_url}/search"

        payload = {
            "query": query,
            "useAutoprompt": use_autoprompt,
            "numResults": num_results,
            "contents": {
                "text": include_text,
                "highlights": include_highlights
            }
        }

        if self.verbose:
            print(f"[SearchService-Exa] 正在搜索: {query}")

        session = await self._get_session()
        timeout_config = aiohttp.ClientTimeout(total=timeout)

        try:
            async with session.post(url, headers=self.headers, json=payload, timeout=timeout_config) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

                return await response.json()

        except asyncio.TimeoutError:
            raise Exception(f"搜索请求超时（超过 {timeout} 秒）")
        except aiohttp.ClientError as e:
            raise Exception(f"网络请求失败: {str(e)}")

    async def search(self,
                     query: str,
                     num_results: int = 3,
                     timeout: int = 30,
                     **kwargs) -> Dict[str, Any]:
        """
        执行异步搜索请求（自动选择搜索引擎）

        Args:
            query: 搜索查询字符串
            num_results: 返回结果数量，默认3条
            timeout: 请求超时时间（秒），默认30秒
            **kwargs: 其他参数

        Returns:
            搜索响应的完整JSON数据
        """
        try:
            if self.engine == 'tavily':
                return await self._search_tavily(query, num_results, timeout=timeout)
            else:
                return await self._search_exa(query, num_results, timeout=timeout, **kwargs)
        except Exception as e:
            if self.verbose:
                print(f"[SearchService] 搜索失败: {e}")
            raise

    def _extract_context_tavily(self, search_response: Dict[str, Any], include_url: bool = False) -> str:
        """从 Tavily 响应中提取上下文"""
        answer = search_response.get('answer', '')
        results = search_response.get('results', [])

        context_parts = []

        # Tavily 提供的总结答案
        if answer:
            context_parts.append(f"【AI总结】\n{answer}\n")

        for i, result in enumerate(results, 1):
            title = result.get('title', '无标题')
            content = result.get('content', '')
            url = result.get('url', '')

            result_context = f"【结果 {i}】\n标题: {title}\n"
            if include_url and url:
                result_context += f"来源: {url}\n"
            if content:
                result_context += f"内容: {content}\n"

            context_parts.append(result_context)

        return "\n".join(context_parts) if context_parts else "未找到相关搜索结果"

    def _extract_context_exa(self, search_response: Dict[str, Any], include_url: bool = False) -> str:
        """从 Exa 响应中提取上下文"""
        results = search_response.get('results', [])

        if not results:
            return "未找到相关搜索结果"

        context_parts = []

        for i, result in enumerate(results, 1):
            title = result.get('title', '无标题')
            highlights = result.get('highlights', [])
            url = result.get('url', '')

            result_context = f"【结果 {i}】\n标题: {title}\n"

            if include_url and url:
                result_context += f"来源: {url}\n"

            if highlights:
                result_context += "相关内容:\n"
                for highlight in highlights:
                    result_context += f"  - {highlight}\n"
            else:
                result_context += "相关内容: 无高亮内容\n"

            context_parts.append(result_context)

        return "\n".join(context_parts)

    def extract_context(self, search_response: Dict[str, Any], include_url: bool = False) -> str:
        """
        从搜索响应中提取有用的上下文信息（自动选择解析方式）

        Args:
            search_response: search() 方法返回的响应数据
            include_url: 是否包含来源URL，默认False

        Returns:
            组合后的上下文文本，格式化为可读字符串
        """
        if self.engine == 'tavily':
            return self._extract_context_tavily(search_response, include_url)
        else:
            return self._extract_context_exa(search_response, include_url)

    async def search_and_extract(self,
                                  query: str,
                                  num_results: int = 3,
                                  include_url: bool = False,
                                  timeout: int = 30) -> str:
        """
        执行异步搜索并直接返回提取的上下文

        Args:
            query: 搜索查询字符串
            num_results: 返回结果数量，默认3条
            include_url: 是否包含来源URL，默认False
            timeout: 请求超时时间（秒），默认30秒

        Returns:
            格式化的上下文文本，失败时返回错误信息
        """
        try:
            response = await self.search(query, num_results=num_results, timeout=timeout)
            return self.extract_context(response, include_url=include_url)
        except Exception as e:
            error_msg = f"搜索失败: {str(e)}"
            if self.verbose:
                print(f"[SearchService] {error_msg}")
            return error_msg

    async def __aenter__(self):
        """支持异步上下文管理器"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出异步上下文管理器时自动关闭 session"""
        await self.close()


async def main():
    """测试函数 - 演示异步搜索服务用法"""
    print("=" * 80)
    print("搜索服务测试 (异步版本)")
    print("=" * 80)

    engine = os.getenv('SEARCH_ENGINE', 'tavily').lower()
    print(f"当前搜索引擎: {engine}")

    try:
        print("\n1. 测试搜索服务 (使用上下文管理器):")
        async with SearchService(verbose=True) as service:
            test_query = "量子计算机最新成果"
            print(f"正在搜索: {test_query}\n")

            context = await service.search_and_extract(test_query, num_results=3)
            print("搜索结果上下文:")
            print("-" * 80)
            print(context)
            print("-" * 80)

    except ValueError as e:
        print(f"\n配置错误: {e}")
        if engine == 'tavily':
            print("提示: 请确保在 .env 文件中设置 TAVILY_API_KEY")
        else:
            print("提示: 请确保在 .env 文件中设置 EXA_API_KEY")
    except Exception as e:
        print(f"\n发生错误: {e}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
