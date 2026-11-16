# Simple desktop edition design (2026-11-13)

## Approved scope
Create an independent copy at C:/Users/Administrator/Desktop/Product Details Generation-2.0. Preserve the original project. Build a simple Chinese Windows GUI and a PyInstaller onedir release containing the Python runtime, libraries, browser driver and Chromium. No user API keys, saved login sessions or real workbook data in the release.

## Desktop behavior
One window: input/output Excel selectors; DeepSeek/Gemini, model and template settings; API key with optional remember-on-this-PC; resume and browser visibility toggles; Alibaba/Taobao/Tmall login; start, graceful stop, progress/log display and open output.
Keep the original product extraction, prompt, generation, title dedupe and Excel schema. Only add optional progress/stop hooks to run.py and scraper.py and a prompt callback to login.py. Existing CLI entry points keep working.
Use Tkinter/ttk. Run acquisition/generation in a separate hidden process of the same executable so the GUI remains responsive and the asyncio event loop is unchanged. Child logs and progress use task files, because a windowed executable has no stdout console.

## Data and packaging
Use %LOCALAPPDATA%/ProductDetailsDesktop for settings, task logs and site login states, with PRODUCT_DETAILS_DATA_DIR override for isolated tests. Store selected input/output as absolute paths. API keys go to the worker environment, not its JSON task file; persist keys only when Remember API Key is selected. Do not put the original .env or session JSON in the copy/release.
The distributable is dist/ProductDetailsDesktop/ProductDetailsDesktop.exe plus _internal/ and a short Chinese usage guide. Browser resources are bundled and resolved from the frozen resource directory. Provide a zip of the whole folder and a blank input template.
Validate current 13 regression tests, new configuration/IPC/stop/login tests, GUI visual layout, and frozen offline browser + two-variant parsing with an isolated PATH/browser cache. Do not call a real AI API while building.

## Runtime contract for independent GUI implementation
desktop_runtime.py exports:
- APP_TITLE = 产品详情生成
- default_settings() -> dict: provider, model, template, input_file, output_file, resume, show_browser, remember_key, api_keys (dict by provider).
- user_data_dir() -> Path; load_settings() -> dict; save_settings(settings) -> None.
- validate_generation(settings, api_key) -> dict normalized with absolute input/output paths, raises ValueError with Chinese message.
- launch_job(settings, *, kind='generate', api_key='', site='alibaba') -> RunningTask.
- RunningTask has folder, log_path, result_path, poll() -> exit code or None, read_updates() -> list[dict], request_stop(), respond(prompt_id, answer), result() -> dict.
- Updates: {type:'log',message:str}; {type:'progress',total:int,done:int,failed:int}; {type:'prompt',id:str,message:str}; {type:'finished',status:'finished'|'stopped'|'error',total:int,done:int,failed:int,message:str,output_file:str}.
- Login prompt containing '(y/n)' or 'y/n' expects 'y'/'n'; other prompts expect empty string after the user has finished browser login.
desktop_app.py exports DesktopApp(tk.Tk). Its guarded main dispatches --worker TASKFILE to desktop_worker.run_worker(TASKFILE); --self-test --report REPORT to desktop_worker.self_test(REPORT); normal mode shows the GUI.
