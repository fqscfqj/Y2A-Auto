#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import uuid
import shutil
import time
import datetime
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, session
from functools import wraps
from flask_cors import CORS, cross_origin
from modules.youtube_handler import download_video_data, extract_video_urls_from_playlist
from modules.utils import parse_id_md_to_json, process_cover, get_app_subdir
from modules.config_manager import load_config, update_config, DEFAULT_CONFIG
from modules.task_manager import add_task, start_task, get_task, get_all_tasks, get_tasks_by_status, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks
from modules.youtube_monitor import youtube_monitor
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.urandom(24)  # 用于flash消息
app.jinja_env.globals.update(now=datetime.now())  # 添加当前时间到模板全局变量
# 登录安全状态存储
def _get_security_state_path():
    try:
        db_dir = get_app_subdir('db')
    except Exception:
        # 回退到当前目录下的db
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'security_state.json')

def _load_security_state():
    path = _get_security_state_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容缺失字段
                if not isinstance(data, dict):
                    data = {}
        else:
            data = {}
    except Exception:
        data = {}
    # 默认值
    return {
        'failed_attempts': int(data.get('failed_attempts', 0) or 0),
        'locked_until': float(data.get('locked_until', 0) or 0.0),
        'last_attempt': float(data.get('last_attempt', 0) or 0.0),
    }

def _save_security_state(state):
    try:
        path = _get_security_state_path()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        if config.get('password_protection_enabled'):
            if 'logged_in' not in session:
                flash('请先登录以访问此页面。', 'info')
                return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# 配置CORS，允许来自YouTube的跨域请求
CORS(app, resources={
    r"/tasks/add_via_extension": {
        "origins": ["*://www.youtube.com", "*://youtube.com", "https://www.youtube.com", "https://youtube.com"],
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# 确保日志目录存在
log_dir = get_app_subdir('logs')
os.makedirs(log_dir, exist_ok=True)

# 配置日志
log_file = os.path.join(log_dir, 'app.log')
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 文件处理器
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.WARNING)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)
# 确保Windows控制台编码正确
if os.name == 'nt':
    import sys
    import codecs
    # 检查Python版本和reconfigure方法可用性
    python_version = sys.version_info
    print(f"DEBUG: 当前Python版本: {python_version.major}.{python_version.minor}.{python_version.micro}")
    print(f"DEBUG: 操作系统: {os.name}, sys.stdout类型: {type(sys.stdout)}")
    
    # 强制设置stdout和stderr为UTF-8编码
    if hasattr(sys.stdout, 'reconfigure'):
        print("DEBUG: sys.stdout.reconfigure方法可用，正在设置UTF-8编码")
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore
            print("DEBUG: sys.stdout.reconfigure执行成功")
        except Exception as e:
            print(f"DEBUG: sys.stdout.reconfigure执行失败: {e}")
    else:
        print("DEBUG: sys.stdout.reconfigure方法不可用，跳过stdout编码设置")
        
    if hasattr(sys.stderr, 'reconfigure'):
        print("DEBUG: sys.stderr.reconfigure方法可用，正在设置UTF-8编码")
        try:
            sys.stderr.reconfigure(encoding='utf-8')  # type: ignore
            print("DEBUG: sys.stderr.reconfigure执行成功")
        except Exception as e:
            print(f"DEBUG: sys.stderr.reconfigure执行失败: {e}")
    else:
        print("DEBUG: sys.stderr.reconfigure方法不可用，跳过stderr编码设置")
    
    # 设置环境变量
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 为控制台处理器设置编码
    try:
        console_handler.setStream(codecs.getwriter('utf-8')(sys.stdout.buffer))  # type: ignore
        print("DEBUG: 控制台处理器编码设置成功")
    except Exception as e:
        print(f"DEBUG: 控制台处理器编码设置失败: {e}")

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
try:
    logging.getLogger().handlers[0].encoding = 'utf-8'  # type: ignore
    print("DEBUG: 第一个日志处理器编码设置成功")
except Exception as e:
    print(f"DEBUG: 第一个日志处理器编码设置失败: {e}")

try:
    if len(logging.getLogger().handlers) > 1:
        logging.getLogger().handlers[1].encoding = 'utf-8'  # type: ignore
        print("DEBUG: 第二个日志处理器编码设置成功")
except Exception as e:
    print(f"DEBUG: 第二个日志处理器编码设置失败: {e}")

# 配置应用日志记录器
logger = logging.getLogger('Y2A-Auto')
logger.setLevel(logging.WARNING)

# 静态目录变量（保留用于兼容性）
covers_dir = os.path.join(get_app_subdir('static'), 'covers')

def init_id_mapping():
    """
    初始化AcFun分区ID映射.
    id_mapping.json 文件现在应该由 acfunid/ 目录直接提供，并包含在Docker镜像中。
    此函数仅记录一条信息，不再执行文件生成或检查逻辑。
    """
    logger.info("AcFun分区ID映射 (id_mapping.json) 应由 'acfunid/' 目录提供。")

# 模板辅助函数
def task_status_display(status):
    """将任务状态代码转换为显示文本"""
    status_map = {
        TASK_STATES['PENDING']: '等待处理',
        TASK_STATES['DOWNLOADING']: '下载中',
        TASK_STATES['DOWNLOADED']: '下载完成',
        TASK_STATES['TRANSLATING_SUBTITLE']: '翻译字幕中',
    TASK_STATES['ASR_TRANSCRIBING']: '语音转写中',
        TASK_STATES['ENCODING_VIDEO']: '转码视频中',
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '准备上传',
        TASK_STATES['UPLOADING']: '上传中',
        TASK_STATES['COMPLETED']: '已完成',
        TASK_STATES['FAILED']: '失败',
        'fetching_info': '采集信息中',
        'info_fetched': '信息已采集',
    }
    return status_map.get(status, status)

def task_status_color(status):
    """将任务状态代码转换为显示颜色"""
    color_map = {
        TASK_STATES['PENDING']: 'secondary',
        TASK_STATES['DOWNLOADING']: 'info',
        TASK_STATES['DOWNLOADED']: 'info',
        TASK_STATES['TRANSLATING_SUBTITLE']: 'info',
    TASK_STATES['ASR_TRANSCRIBING']: 'info',
        TASK_STATES['ENCODING_VIDEO']: 'info',
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
        TASK_STATES['READY_FOR_UPLOAD']: 'primary',
        TASK_STATES['UPLOADING']: 'primary',
        TASK_STATES['COMPLETED']: 'success',
        TASK_STATES['FAILED']: 'danger'
    }
    return color_map.get(status, 'secondary')

def get_partition_name(partition_id):
    """根据分区ID获取分区名称"""
    if not partition_id:
        return None
    
    # 读取分区映射数据 - 修改路径为 acfunid
    id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)
            
        # 遍历查找匹配的分区ID
        for category in id_mapping:
            for partition in category.get('partitions', []):
                if partition.get('id') == partition_id:
                    return partition.get('name')
                
                # 检查子分区
                for sub_partition in partition.get('sub_partitions', []):
                    if sub_partition.get('id') == partition_id:
                        return sub_partition.get('name')
    except Exception as e:
        logger.error(f"获取分区名称时出错: {str(e)}")
    
    return None

def parse_json(json_str):
    """将JSON字符串解析为Python对象"""
    if not json_str:
        return {}  # 返回空字典
    
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"解析JSON时出错: {str(e)}")
        return {} # 返回空字典

def parse_youtube_duration(duration_str):
    """解析YouTube ISO 8601时长格式为秒数"""
    import re
    
    if not duration_str:
        return 0
    
    # PT1H30M45S -> 1小时30分45秒
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration_str)
    
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json
)

# 注册模板过滤器
app.jinja_env.filters['parse_youtube_duration'] = parse_youtube_duration

ALIYUN_LABEL_MAP = {
    "pornographic_adult": "疑似色情内容",
    "sexual_terms": "疑似性健康内容",
    "sexual_suggestive": "疑似低俗内容",
    "political_figure": "疑似政治人物",
    "political_entity": "疑似政治实体",
    "political_n": "疑似敏感政治内容",
    "political_p": "疑似涉政禁宣人物",
    "political_a": "涉政专项升级保障",
    "violent_extremist": "疑似极端组织",
    "violent_incidents": "疑似极端主义内容",
    "violent_weapons": "疑似武器弹药",
    "contraband_drug": "疑似毒品相关",
    "contraband_gambling": "疑似赌博相关",
    "contraband_act": "疑似违禁行为",
    "contraband_entity": "疑似违禁工具",
    "inappropriate_discrimination": "疑似偏见歧视内容",
    "inappropriate_ethics": "疑似不良价值观内容",
    "inappropriate_profanity": "疑似攻击辱骂内容",
    "inappropriate_oral": "疑似低俗口头语内容",
    "inappropriate_superstition": "疑似封建迷信内容",
    "inappropriate_nonsense": "疑似无意义灌水内容",
    "pt_to_sites": "疑似站外引流",
    "pt_by_recruitment": "疑似网赚兼职广告",
    "pt_to_contact": "疑似引流广告号",
    "religion_b": "疑似涉及佛教",
    "religion_t": "疑似涉及道教",
    "religion_c": "疑似涉及基督教",
    "religion_i": "疑似涉及伊斯兰教",
    "religion_h": "疑似涉及印度教",
    "customized": "命中自定义词库",
    "nonLabel": "内容正常", # 通常表示无风险
    "normal": "内容正常" # 另一种表示无风险的标签
    # 可以根据需要添加更多映射
}

def get_aliyun_label_chinese(label_value):
    """获取阿里云审核标签的中文含义"""
    return ALIYUN_LABEL_MAP.get(label_value, label_value) # 如果找不到映射，返回原始标签

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json,
    get_aliyun_label_chinese=get_aliyun_label_chinese # 添加新的辅助函数
)

@app.route('/login', methods=['GET', 'POST'])
def login():
    config = load_config()
    # 如果密码保护未启用，或已登录，则重定向到首页
    if not config.get('password_protection_enabled'):
        return redirect(url_for('index'))
    if 'logged_in' in session:
        return redirect(url_for('index'))

    # 读取登录安全状态
    sec = _load_security_state()
    now_ts = time.time()
    # 检查是否处于锁定期
    if sec.get('locked_until', 0) and now_ts < sec['locked_until']:
        remaining = int(sec['locked_until'] - now_ts)
        minutes = remaining // 60
        seconds = remaining % 60
        flash(f'登录已被临时锁定，请 {minutes} 分 {seconds} 秒后重试。', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        password = request.form.get('password')
        stored_password = config.get('password')

        # 检查是否有设置密码
        if not stored_password:
            flash('系统尚未设置密码，无法登录。请在禁用密码保护的情况下，进入设置页面设置密码。', 'danger')
            return render_template('login.html')

        if password and password == stored_password:
            session['logged_in'] = True
            session.permanent = True  # session持久化
            # 登录成功，重置失败计数与锁定
            sec.update({'failed_attempts': 0, 'locked_until': 0, 'last_attempt': now_ts})
            _save_security_state(sec)
            flash('登录成功', 'success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            # 密码错误，更新失败计数
            max_attempts = int(config.get('LOGIN_MAX_FAILED_ATTEMPTS', 5) or 5)
            lock_minutes = int(config.get('LOGIN_LOCKOUT_MINUTES', 15) or 15)
            failed = int(sec.get('failed_attempts', 0) or 0) + 1
            sec['failed_attempts'] = failed
            sec['last_attempt'] = now_ts
            # 达到阈值则锁定
            if failed >= max_attempts:
                sec['locked_until'] = now_ts + lock_minutes * 60
                _save_security_state(sec)
                flash(f'密码错误次数过多（{failed}/{max_attempts}），已锁定 {lock_minutes} 分钟。', 'danger')
            else:
                _save_security_state(sec)
                remain = max_attempts - failed
                flash(f'密码错误。还可尝试 {remain} 次后将被锁定。', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('您已成功退出。', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """首页"""
    logger.info("访问首页")
    # 统计信息用于仪表盘
    try:
        from modules.task_manager import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()

        # 本地时间的今日起止
        now_local = datetime.now()
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        fmt = "%Y-%m-%d %H:%M:%S"
        start_str = today_start.strftime(fmt)
        end_str = tomorrow_start.strftime(fmt)

        # 各类计数
        cur.execute("SELECT COUNT(*) FROM tasks")
        total_tasks = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['AWAITING_REVIEW'],))
        awaiting_review = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['FAILED'],))
        failed_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['PENDING'],))
        pending_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['READY_FOR_UPLOAD'],))
        ready_total = cur.fetchone()[0] or 0

        # 进行中的状态集合
        processing_states = (
            'fetching_info', 'info_fetched',
            TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'], TASK_STATES['PARTITIONING'],
            TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'], TASK_STATES['DOWNLOADED'],
            TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'],
            TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING']
        )
        placeholders = ",".join(["?"] * len(processing_states))
        cur.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})", processing_states)
        in_progress = cur.fetchone()[0] or 0

        # 今日完成/失败/新增任务
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['COMPLETED'], start_str, end_str)
        )
        completed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['FAILED'], start_str, end_str)
        )
        failed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at < ?",
            (start_str, end_str)
        )
        created_today = cur.fetchone()[0] or 0

        # 最近任务（按更新时间倒序）
        cur.execute(
            "SELECT id, video_title_translated, video_title_original, status, updated_at, acfun_upload_response FROM tasks ORDER BY updated_at DESC LIMIT 10"
        )
        rows = cur.fetchall()
        recent_tasks = []
        for r in rows:
            ac_number = None
            try:
                resp = json.loads(r[5]) if r[5] else None
                if isinstance(resp, dict):
                    ac_number = resp.get('ac_number')
            except Exception:
                ac_number = None
            recent_tasks.append({
                'id': r[0],
                'title': r[1] or r[2] or '未获取标题',
                'status': r[3],
                'updated_at': r[4],
                'ac_number': ac_number
            })

        conn.close()

        stats = {
            'total_tasks': total_tasks,
            'awaiting_review': awaiting_review,
            'failed_total': failed_total,
            'pending_total': pending_total,
            'ready_total': ready_total,
            'in_progress': in_progress,
            'completed_today': completed_today,
            'failed_today': failed_today,
            'created_today': created_today
        }
    except Exception as e:
        logger.warning(f"首页统计失败: {e}")
        stats = {
            'total_tasks': 0,
            'awaiting_review': 0,
            'failed_total': 0,
            'pending_total': 0,
            'ready_total': 0,
            'in_progress': 0,
            'completed_today': 0,
            'failed_today': 0,
            'created_today': 0
        }
        recent_tasks = []

    return render_template('index.html', stats=stats, recent_tasks=recent_tasks)

@app.route('/tasks')
@login_required
def tasks():
    """任务列表页面"""
    logger.info("访问任务列表页面")
    tasks_list = get_all_tasks()
    return render_template('tasks.html', tasks=tasks_list)

@app.route('/manual_review')
@login_required
def manual_review():
    """人工审核列表页面"""
    logger.info("访问人工审核列表页面")
    review_tasks = get_tasks_by_status(TASK_STATES['AWAITING_REVIEW'])
    
    # 封面图片现在直接从downloads目录提供
    
    return render_template('manual_review.html', tasks=review_tasks)

@app.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    """任务编辑页面"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if request.method == 'POST':
        # 处理表单提交
        video_title = request.form.get('video_title_translated', '')
        description = request.form.get('description_translated', '')
        partition_id = request.form.get('selected_partition_id', '')
        tags_json = request.form.get('tags_json', '[]')
        
        # 更新任务信息
        update_data = {
            'video_title_translated': video_title,
            'description_translated': description,
            'selected_partition_id': partition_id,
            'tags_generated': tags_json
        }
        
        if task['status'] == TASK_STATES['AWAITING_REVIEW']:
            # 如果是从等待审核状态修改，则设置为等待上传
            update_data['status'] = TASK_STATES['PENDING']
        
        # 调试：检查update_task函数参数类型
        print(f"DEBUG: update_task参数 - task_id: {type(task_id)}, update_data: {type(update_data)}")
        print(f"DEBUG: update_data内容: {update_data}")
        # 检查是否有silent参数类型问题
        if 'silent' in update_data:
            print(f"DEBUG: silent参数值: {update_data['silent']}, 类型: {type(update_data['silent'])}")
        try:
            # 确保silent参数是布尔类型
            final_update_data = update_data.copy()
            silent_param = False  # 默认值
            
            if 'silent' in final_update_data:
                if isinstance(final_update_data['silent'], str):
                    silent_param = final_update_data['silent'].lower() in ('true', 'yes', '1', 'on')
                elif isinstance(final_update_data['silent'], bool):
                    silent_param = final_update_data['silent']
                # 从final_update_data中移除silent，避免重复传递
                final_update_data.pop('silent')
            
            update_task(task_id, silent=silent_param, **final_update_data)
            print("DEBUG: update_task调用成功")
        except Exception as e:
            print(f"DEBUG: update_task调用失败: {e}")
        logger.info(f"任务 {task_id} 信息已更新")
        
        # 执行上传操作（当任务处于"已完成"、"等待处理"或"准备上传"状态时）
        task = get_task(task_id)  # 重新获取更新后的任务信息
        if task and task['status'] in [TASK_STATES['COMPLETED'], TASK_STATES['PENDING'], TASK_STATES['READY_FOR_UPLOAD']]:
            # 获取当前配置
            config = load_config()
            
            # 启动后台上传
            logger.info(f"开始后台上传任务 {task_id} 到AcFun")
            flash('任务已保存，正在后台上传到AcFun...', 'info')
            
            import threading
            
            def background_upload():
                """后台上传函数"""
                try:
                    # 调用上传函数
                    success = force_upload_task(task_id, config)
                    
                    if success:
                        logger.info(f"任务 {task_id} 后台上传成功")
                    else:
                        logger.error(f"任务 {task_id} 后台上传失败")
                except Exception as e:
                    logger.error(f"任务 {task_id} 后台上传出错: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # 启动后台线程
            upload_thread = threading.Thread(target=background_upload, daemon=True)
            upload_thread.start()
        else:
            flash('任务已保存，但尚未完成处理，无法上传', 'warning')
        
        return redirect(url_for('tasks'))
    
    # GET请求，显示编辑页面
    # 封面图片现在直接从downloads目录提供
    
    # 读取分区映射数据
    id_mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'acfunid', 'id_mapping.json')
    id_mapping = []
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)
    except Exception as e:
        logger.error(f"读取分区映射数据失败: {str(e)}")
    
    # 准备标签字符串
    tags_string = ""
    if task.get('tags_generated'):
        try:
            tags = json.loads(task['tags_generated'])
            tags_string = ", ".join(tags)
        except Exception as e:
            logger.error(f"解析标签JSON失败: {str(e)}")
    
    # 获取当前配置
    config = load_config()
    
    return render_template(
        'edit_task.html', 
        task=task, 
        id_mapping=id_mapping, 
        tags_string=tags_string,
        config=config
    )

@app.route('/tasks/<task_id>/review')
@login_required
def review_task(task_id):
    """重定向到任务编辑页面"""
    return redirect(url_for('edit_task', task_id=task_id))

@app.route('/tasks/add', methods=['POST'])
@login_required
def add_task_route():
    """添加新任务，支持播放列表批量添加"""
    youtube_url = request.form.get('youtube_url')
    
    if not youtube_url:
        flash('YouTube URL不能为空', 'danger')
        return redirect(url_for('tasks'))

    # 判断是否为播放列表URL
    if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
        # 提取所有视频URL
        config = load_config()
        cookies_path = config.get('YOUTUBE_COOKIES_PATH')
        video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
        if not video_urls:
            flash('未能提取到播放列表中的视频', 'danger')
            return redirect(url_for('tasks'))
        added_count = 0
        for url in video_urls:
            task_id = add_task(url)
            if task_id:
                added_count += 1
        flash(f'已批量添加 {added_count} 个视频任务（来自播放列表）', 'success')
        return redirect(url_for('tasks'))
    else:
        task_id = add_task(youtube_url)
        if task_id:
            config = load_config()
            if config.get('AUTO_MODE_ENABLED', False):
                logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                start_task(task_id, config)
                flash(f'任务已添加并开始处理: {youtube_url}', 'success')
            else:
                flash(f'任务已添加: {youtube_url}', 'success')
        else:
            flash(f'添加任务失败: {youtube_url}', 'danger')
        return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/start', methods=['POST'])
@login_required
def start_task_route(task_id):
    """开始处理任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
        flash(f'当前任务状态为 {task_status_display(task["status"])}，不能启动', 'warning')
        return redirect(url_for('tasks'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动任务处理
    success = start_task(task_id, config)
    
    if success:
        # 检查是否是自动模式
        if config.get('AUTO_MODE_ENABLED', False):
            flash('任务已启动，自动模式将会自动完成下载、处理和上传', 'info')
            
            # 等待一段时间后检查任务状态
            import threading
            
            # 使用传统页面刷新方式
        else:
            flash('任务处理已启动', 'success')
    else:
        flash('启动任务处理失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task_route(task_id):
    """删除任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = delete_task(task_id, delete_files)
    
    if success:
        flash('任务已删除', 'success')
    else:
        flash('删除任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/force_upload', methods=['POST'])
@login_required
def force_upload_task_route(task_id):
    """强制上传任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('manual_review'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动后台强制上传
    logger.info(f"开始后台强制上传任务 {task_id} 到AcFun")
    flash('已启动强制上传，正在后台处理...', 'info')
    
    import threading
    
    def background_force_upload():
        """后台强制上传函数"""
        try:
            # 调用上传函数
            success = force_upload_task(task_id, config)
                
            if success:
                logger.info(f"任务 {task_id} 后台强制上传成功")
            else:
                logger.error(f"任务 {task_id} 后台强制上传失败")
        except Exception as e:
            logger.error(f"任务 {task_id} 后台强制上传出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
    
    # 启动后台线程
    upload_thread = threading.Thread(target=background_force_upload, daemon=True)
    upload_thread.start()
    
    return redirect(url_for('manual_review'))

@app.route('/tasks/reset_stuck', methods=['POST'])
@login_required
def reset_stuck_tasks_route():
    """重置卡住的任务"""
    from modules.task_manager import reset_stuck_tasks
    
    try:
        reset_count = reset_stuck_tasks()
        if reset_count > 0:
            flash(f'已重置 {reset_count} 个卡住的任务', 'success')
        else:
            flash('没有发现卡住的任务', 'info')
    except Exception as e:
        logger.error(f"重置卡住任务失败: {str(e)}")
        flash('重置卡住任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/abandon', methods=['POST'])
@login_required
def abandon_task_route(task_id):
    """放弃任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    # 更新任务状态为失败
    update_task(task_id, status=TASK_STATES['FAILED'], error_message="用户主动放弃任务")
    
    if delete_files:
        # 删除任务文件
        from modules.task_manager import delete_task_files
        delete_task_files(task_id)
    
    flash('任务已废弃', 'success')
    return redirect(url_for('tasks'))

# 系统健康检查辅助函数

def check_docker_volumes():
    """检查Docker挂载卷状态"""
    volumes = {}
    app_root = os.path.dirname(os.path.abspath(__file__))
    
    volume_paths = [
        ('config', 'config'),
        ('db', 'db'),
        ('downloads', 'downloads'),
        ('logs', 'logs'),
        ('cookies', 'cookies'),
        ('temp', 'temp')
    ]
    
    for name, path in volume_paths:
        full_path = os.path.join(app_root, path)
        volumes[name] = {
            'path': full_path,
            'exists': os.path.exists(full_path),
            'is_mount': os.path.ismount(full_path),
            'writable': os.access(full_path, os.W_OK) if os.path.exists(full_path) else False,
            'size_mb': get_directory_size(full_path) if os.path.exists(full_path) else 0
        }
    
    return volumes

def get_directory_size(path):
    """获取目录大小(MB)"""
    try:
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
        return round(total_size / 1024 / 1024, 2)
    except:
        return 0

def get_database_info():
    """获取数据库文件信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            return {
                'path': db_path,
                'size': stat_info.st_size,
                'writable': os.access(db_path, os.W_OK),
                'last_modified': stat_info.st_mtime
            }
        else:
            return {
                'path': db_path,
                'size': 0,
                'writable': False,
                'last_modified': None
            }
    except Exception as e:
        return {
            'path': 'unknown',
            'size': 0,
            'writable': False,
            'error': str(e)
        }

def get_database_debug_info():
    """获取数据库调试信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        debug_info = {
            'db_path': db_path,
            'db_exists': os.path.exists(db_path),
            'db_dir': os.path.dirname(db_path),
            'db_dir_exists': os.path.exists(os.path.dirname(db_path)),
            'db_dir_writable': os.access(os.path.dirname(db_path), os.W_OK) if os.path.exists(os.path.dirname(db_path)) else False,
            'current_user': os.environ.get('USER', 'unknown'),
            'current_uid': os.getuid() if hasattr(os, 'getuid') else 'unknown',  # type: ignore
            'current_gid': os.getgid() if hasattr(os, 'getgid') else 'unknown'   # type: ignore
        }
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            debug_info.update({
                'db_size': stat_info.st_size,
                'db_mode': oct(stat_info.st_mode)[-3:],
                'db_uid': stat_info.st_uid,
                'db_gid': stat_info.st_gid
            })
        
        return debug_info
    except Exception as e:
        return {'error': str(e)}

def get_file_info(file_path):
    """获取文件详细信息"""
    try:
        info = {
            'exists': os.path.exists(file_path),
            'size': 0,
            'readable': False,
            'last_modified': None
        }
        
        if info['exists']:
            stat_info = os.stat(file_path)
            info.update({
                'size': stat_info.st_size,
                'readable': os.access(file_path, os.R_OK),
                'last_modified': stat_info.st_mtime
            })
        
        return info
    except Exception as e:
        return {
            'exists': False,
            'size': 0,
            'readable': False,
            'last_modified': None,
            'error': str(e)
        }

def get_path_debug_info(file_path):
    """获取路径调试信息"""
    try:
        debug_info = {
            'path': file_path,
            'dirname': os.path.dirname(file_path),
            'basename': os.path.basename(file_path),
            'dirname_exists': os.path.exists(os.path.dirname(file_path)),
            'dirname_readable': os.access(os.path.dirname(file_path), os.R_OK) if os.path.exists(os.path.dirname(file_path)) else False,
            'dirname_writable': os.access(os.path.dirname(file_path), os.W_OK) if os.path.exists(os.path.dirname(file_path)) else False
        }
        
        # 列出目录内容
        if debug_info['dirname_exists'] and debug_info['dirname_readable']:
            try:
                debug_info['directory_contents'] = os.listdir(os.path.dirname(file_path))
            except:
                debug_info['directory_contents'] = 'permission_denied'
        
        return debug_info
    except Exception as e:
        return {'error': str(e)}

@app.route('/system_health')
def system_health():
    """系统健康检查 - 增强Docker环境兼容性"""
    from modules.task_manager import get_db_connection, validate_cookies
    import sqlite3
    import os
    import platform
    import sys
    
    # 检测运行环境
    is_docker = os.path.exists('/.dockerenv') or os.environ.get('CONTAINER') == 'docker'
    
    health_status = {
        'environment': {
            'platform': platform.system(),
            'python_version': sys.version.split()[0],
            'is_docker': is_docker,
            'user': os.environ.get('USER', 'unknown'),
            'working_directory': os.getcwd()
        },
        'database': {'status': 'unknown', 'message': ''},
        'youtube_cookies': {'status': 'unknown', 'message': ''},
        'acfun_cookies': {'status': 'unknown', 'message': ''},
        'stuck_tasks': {'count': 0, 'tasks': []},
        'recent_errors': [],
        'docker_volumes': {}
    }
    
    # Docker环境特殊检查
    if is_docker:
        health_status['docker_volumes'] = check_docker_volumes()
    
    # 检查数据库
    try:
        logger.info("开始数据库健康检查...")
        conn = get_db_connection()
        
        # 测试基本连接
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        task_count = cursor.fetchone()[0]
        
        # 检查数据库文件权限和位置
        db_info = get_database_info()
        
        health_status['database'] = {
            'status': 'ok',
            'message': f'数据库正常，共有 {task_count} 个任务',
            'location': db_info['path'],
            'size_mb': round(db_info['size'] / 1024 / 1024, 2),
            'writable': db_info['writable']
        }
        
        # 检查卡住的任务
        stuck_cursor = conn.execute('''
            SELECT id, status, created_at, updated_at, error_message
            FROM tasks 
            WHERE status IN ('processing', 'downloading', 'uploading', 'fetching_info', 'translating')
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        ''')
        stuck_tasks = stuck_cursor.fetchall()
        health_status['stuck_tasks'] = {
            'count': len(stuck_tasks),
            'tasks': [{'id': t[0][:8] + '...', 'status': t[1], 'updated': t[3]} for t in stuck_tasks]
        }
        
        # 检查最近的错误
        error_cursor = conn.execute('''
            SELECT id, error_message, updated_at
            FROM tasks 
            WHERE status = 'failed' AND error_message IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 5
        ''')
        error_tasks = error_cursor.fetchall()
        health_status['recent_errors'] = [
            {'id': t[0][:8] + '...', 'error': t[1][:100] + '...' if len(t[1]) > 100 else t[1], 'time': t[2]}
            for t in error_tasks
        ]
        
        conn.close()
        logger.info("数据库健康检查完成")
    except Exception as e:
        logger.error(f"数据库健康检查失败: {str(e)}")
        health_status['database'] = {
            'status': 'error',
            'message': f'数据库错误: {str(e)}',
            'details': get_database_debug_info()
        }
    
    # 检查cookies - 使用更健壮的路径处理
    try:
        logger.info("开始cookies健康检查...")
        config = load_config()
        
        # 获取应用根目录
        app_root = os.path.dirname(os.path.abspath(__file__))
        
        # YouTube cookies
        yt_cookies_path = config.get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt')
        if yt_cookies_path:
            # 如果是相对路径，转换为绝对路径
            if not os.path.isabs(yt_cookies_path):
                yt_cookies_path = os.path.join(app_root, yt_cookies_path)
            
            try:
                logger.debug(f"检查YouTube cookies文件: {yt_cookies_path}")
                is_valid, message = validate_cookies(yt_cookies_path, "YouTube")
                
                # 获取文件详细信息
                file_info = get_file_info(yt_cookies_path)
                
                health_status['youtube_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': yt_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception as e:
                logger.error(f"YouTube cookies检查异常: {str(e)}")
                health_status['youtube_cookies'] = {
                    'status': 'error',
                    'message': f'检查失败: {str(e)}',
                    'path': yt_cookies_path,
                    'debug_info': get_path_debug_info(yt_cookies_path)
                }
        else:
            health_status['youtube_cookies'] = {
                'status': 'warning',
                'message': '未配置YouTube cookies路径'
            }
        
        # AcFun cookies
        ac_cookies_path = config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.txt')
        if ac_cookies_path:
            # 如果是相对路径，转换为绝对路径
            if not os.path.isabs(ac_cookies_path):
                ac_cookies_path = os.path.join(app_root, ac_cookies_path)
            
            try:
                logger.debug(f"检查AcFun cookies文件: {ac_cookies_path}")
                is_valid, message = validate_cookies(ac_cookies_path, "AcFun")
                
                # 获取文件详细信息
                file_info = get_file_info(ac_cookies_path)
                
                health_status['acfun_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': ac_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception as e:
                logger.error(f"AcFun cookies检查异常: {str(e)}")
                health_status['acfun_cookies'] = {
                    'status': 'error',
                    'message': f'检查失败: {str(e)}',
                    'path': ac_cookies_path,
                    'debug_info': get_path_debug_info(ac_cookies_path)
                }
        else:
            health_status['acfun_cookies'] = {
                'status': 'warning',
                'message': '未配置AcFun cookies路径'
            }
        
        logger.info("cookies健康检查完成")
            
    except Exception as e:
        logger.error(f"检查cookies时发生错误: {str(e)}")
        health_status['youtube_cookies'] = {
            'status': 'error',
            'message': f'检查失败: {str(e)}',
            'debug_info': str(e)
        }
        health_status['acfun_cookies'] = {
            'status': 'error',
            'message': f'检查失败: {str(e)}',
            'debug_info': str(e)
        }
    
    return jsonify(health_status)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """设置页面，用于管理配置"""
    if request.method == 'POST':
        # 处理表单提交
        form_data = request.form.to_dict()
        
        # --- 密码保护 ---
        # 处理密码更新
        new_password = form_data.get('new_password')
        confirm_password = form_data.get('confirm_password')

        if new_password:
            if new_password == confirm_password:
                form_data['password'] = new_password
            else:
                flash('新密码两次输入不一致，密码未更新。', 'danger')
        
        # 从字典中移除密码字段，防止重复保存
        form_data.pop('new_password', None)
        form_data.pop('confirm_password', None)

        # 处理复选框（未选中时不会提交）
        checkboxes = [
            'AUTO_MODE_ENABLED', 'TRANSLATE_TITLE', 'TRANSLATE_DESCRIPTION',
            'GENERATE_TAGS', 'RECOMMEND_PARTITION', 'CONTENT_MODERATION_ENABLED',
            'LOG_CLEANUP_ENABLED', 'SUBTITLE_TRANSLATION_ENABLED', 'SUBTITLE_EMBED_IN_VIDEO',
            'SUBTITLE_KEEP_ORIGINAL', 'YOUTUBE_PROXY_ENABLED', 'password_protection_enabled',
            'SPEECH_RECOGNITION_ENABLED', 'SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED'
        ]
        for checkbox in checkboxes:
            if checkbox not in form_data:
                form_data[checkbox] = 'off'  # 未选中的复选框
        
        # 处理数字类型的配置项
        numeric_fields = [
            'MAX_CONCURRENT_TASKS', 'MAX_CONCURRENT_UPLOADS', 'LOG_CLEANUP_HOURS',
            'LOG_CLEANUP_INTERVAL', 'SUBTITLE_BATCH_SIZE', 'SUBTITLE_MAX_RETRIES',
            'SUBTITLE_RETRY_DELAY', 'SUBTITLE_MAX_WORKERS', 'YOUTUBE_DOWNLOAD_THREADS',
            'LOGIN_MAX_FAILED_ATTEMPTS', 'LOGIN_LOCKOUT_MINUTES',
            'SPEECH_RECOGNITION_MIN_SUBTITLE_LINES'
        ]
        for field in numeric_fields:
            if field in form_data:
                try:
                    print(f"DEBUG: 转换前 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                    original_value = form_data[field]
                    form_data[field] = str(int(form_data[field]))  # 转换为int再转回str以满足类型要求
                    print(f"DEBUG: 转换后 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                except (ValueError, TypeError) as e:
                    print(f"DEBUG: 转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    # 如果转换失败，使用默认值
                    defaults = {
                        'MAX_CONCURRENT_TASKS': 3,
                        'MAX_CONCURRENT_UPLOADS': 1,
                        'LOG_CLEANUP_HOURS': 168,
                        'LOG_CLEANUP_INTERVAL': 24,
                        'SUBTITLE_BATCH_SIZE': 5,
                        'SUBTITLE_MAX_RETRIES': 3,
                        'SUBTITLE_RETRY_DELAY': 5,
                        'SUBTITLE_MAX_WORKERS': 0,
                        'YOUTUBE_DOWNLOAD_THREADS': 4,
                        'LOGIN_MAX_FAILED_ATTEMPTS': 5,
                        'LOGIN_LOCKOUT_MINUTES': 15
                    }
                    form_data[field] = str(defaults.get(field, 1))  # 转换为str以满足类型要求
                    print(f"DEBUG: 使用默认值 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
        
        # 处理文件上传
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        # 处理YouTube Cookies文件上传
        if 'youtube_cookies_file' in request.files:
            cookies_file = request.files['youtube_cookies_file']
            if cookies_file.filename:
                # 保存到cookies目录
                yt_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
                cookies_file.save(yt_cookies_path)
                
                # 更新配置中的路径
                form_data['YOUTUBE_COOKIES_PATH'] = 'cookies/yt_cookies.txt'
                
                logger.info(f"YouTube cookies文件已上传并保存到: {yt_cookies_path}")
        
        # 处理AcFun Cookies文件上传
        if 'acfun_cookies_file' in request.files:
            cookies_file = request.files['acfun_cookies_file']
            if cookies_file.filename:
                # 保存到cookies目录
                ac_cookies_path = os.path.join(cookies_dir, 'ac_cookies.txt')
                cookies_file.save(ac_cookies_path)
                
                # 更新配置中的路径
                form_data['ACFUN_COOKIES_PATH'] = 'cookies/ac_cookies.txt'
                
                logger.info(f"AcFun cookies文件已上传并保存到: {ac_cookies_path}")
        
        # 更新配置
        updated_config = update_config(form_data)
        
        # 始终同步到应用配置并刷新任务处理器配置（若配置变更则内部会安全重建）
        try:
            from modules.task_manager import get_global_task_processor
            app.config['Y2A_SETTINGS'] = updated_config
            # 防御：即使配置中的数字字段是字符串也不致崩溃
            get_global_task_processor(updated_config)
            logger.info("配置已更新并同步到任务处理器")
        except Exception as e:
            logger.warning(f"同步任务处理器配置失败: {e}")
        
        # 如果YouTube API密钥有更新，同步到监控系统
        if 'YOUTUBE_API_KEY' in form_data:
            api_key = form_data['YOUTUBE_API_KEY']
            if api_key:
                youtube_monitor.set_api_key(api_key)
                logger.info("YouTube API密钥已更新并同步到监控系统")
        
        flash('配置已成功保存', 'success')
        return redirect(url_for('settings'))
    
    # GET请求，显示设置页面
    config = load_config()
    return render_template('settings.html', config=config)

@app.route('/settings/update_cover_mode', methods=['POST'])
@login_required
def update_cover_mode():
    """更新封面处理模式"""
    try:
        data = request.get_json()
        mode = data.get('mode')
        task_id = data.get('task_id')
        
        if mode not in ('crop', 'pad'):
            return jsonify({"success": False, "message": "无效的处理模式"}), 400
        
        # 更新全局配置
        config = load_config()
        config['COVER_PROCESSING_MODE'] = mode
        update_config(config)
        
        # 如果提供了任务ID，则处理该任务的封面
        if task_id:
            task = get_task(task_id)
            if task and task.get('cover_path_local') and os.path.exists(task['cover_path_local']):
                # 直接在downloads目录中处理封面
                process_cover(task['cover_path_local'], mode=mode)
        
        return jsonify({"success": True, "message": "封面处理模式已更新"})
    except Exception as e:
        logger.error(f"更新封面处理模式失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/test_download')
@login_required
def test_download():
    """测试YouTube视频下载功能"""
    url = request.args.get('url')
    config = load_config()
    cookies_path = config.get('YOUTUBE_COOKIES_PATH')
    
    if not url:
        return jsonify({"success": False, "error": "缺少url参数"}), 400
    
    # 创建唯一任务ID
    task_id = str(uuid.uuid4())
    logger.info(f"开始测试下载，URL: {url}, 任务ID: {task_id}")
    
    # 调用下载函数
    success, result = download_video_data(url, task_id, cookies_path)
    
    if success:
        return jsonify({
            "success": True,
            "task_id": task_id,
            "result": {
                "video_path": os.path.basename(result["video_path"]),
                "metadata_path": os.path.basename(result["metadata_path"]) if result["metadata_path"] else None,
                "cover_path": os.path.basename(result["cover_path"]) if result["cover_path"] else None,
                "subtitles_paths": [os.path.basename(p) for p in result["subtitles_paths"]],
                "task_dir": result["task_dir"]
            }
        })
    else:
        return jsonify({"success": False, "error": result}), 500

@app.route('/tasks/add_via_extension', methods=['POST', 'OPTIONS'])
@cross_origin(origins=["*://www.youtube.com", "*://youtube.com", "https://www.youtube.com", "https://youtube.com"])
def add_task_via_extension():
    """
    接收来自浏览器扩展的任务添加请求，支持播放列表批量添加
    """
    config = load_config()
    if config.get('password_protection_enabled'):
        if 'logged_in' not in session:
            return jsonify({
                "success": False,
                "message": "需要登录",
                "action": "login_required"
            }), 401

    if request.method == 'OPTIONS':
        return '', 204
    try:
        data = request.get_json()
        if not data or 'youtube_url' not in data:
            logger.error("请求数据无效或缺少youtube_url")
            return jsonify({
                "success": False,
                "message": "请求格式错误，缺少youtube_url字段"
            }), 400
        youtube_url = data['youtube_url']
        logger.info(f"从浏览器扩展接收到添加任务请求: {youtube_url}")
        # 判断是否为播放列表URL
        if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
            cookies_path = config.get('YOUTUBE_COOKIES_PATH')
            video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
            if not video_urls:
                return jsonify({
                    "success": False,
                    "message": "未能提取到播放列表中的视频"
                }), 400
            added_count = 0
            for url in video_urls:
                task_id = add_task(url)
                if task_id:
                    added_count += 1
            return jsonify({
                "success": True,
                "message": f"已批量添加 {added_count} 个视频任务（来自播放列表）",
                "added_count": added_count
            })
        else:
            task_id = add_task(youtube_url)
            if task_id:
                config = load_config()
                if config.get('AUTO_MODE_ENABLED', False):
                    logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                    start_task(task_id, config)
                    status_message = "任务已添加并开始处理"
                else:
                    status_message = "任务已添加到队列"
                return jsonify({
                    "success": True,
                    "message": status_message,
                    "task_id": task_id
                })
            else:
                return jsonify({
                    "success": False,
                    "message": "添加任务失败，请检查服务器日志"
                }), 500
    except Exception as e:
        logger.error(f"处理扩展请求时发生错误: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"处理请求时发生错误: {str(e)}"
        }), 500

@app.route('/tasks/clear_all', methods=['POST'])
@login_required
def clear_all_tasks_route():
    """清除所有任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = clear_all_tasks(delete_files)
    
    if success:
        flash('所有任务已清除', 'success')
    else:
        flash('清除所有任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/covers/<task_id>')
@login_required
def get_task_cover(task_id):
    """获取任务封面图片"""
    try:
        # 获取任务信息
        task = get_task(task_id)
        if not task:
            logger.warning(f"任务 {task_id} 不存在")
            return '', 404
        
        # 检查封面文件路径
        cover_path = task.get('cover_path_local')
        if not cover_path or not os.path.exists(cover_path):
            logger.warning(f"任务 {task_id} 的封面文件不存在: {cover_path}")
            return '', 404
        
        # 直接从downloads目录提供文件
        return send_file(cover_path, mimetype='image/jpeg')
        
    except Exception as e:
        logger.error(f"获取任务 {task_id} 封面时出错: {str(e)}")
        return '', 500

# 配置app
def configure_app(app, config_data):
    """
    配置Flask应用
    
    Args:
        app: Flask应用实例
        config_data: 配置数据
    """
    app.config['OPENAI_API_KEY'] = config_data.get('OPENAI_API_KEY', '')
    app.config['OPENAI_BASE_URL'] = config_data.get('OPENAI_BASE_URL', '')
    app.config['OPENAI_MODEL_NAME'] = config_data.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
    
    # 不再使用proxies参数，新版OpenAI客户端不支持
    # 如果需要代理，通过环境变量设置: HTTP_PROXY, HTTPS_PROXY

    app.config['ALIYUN_ACCESS_KEY_ID'] = config_data.get('ALIYUN_ACCESS_KEY_ID', '')
    app.config['ALIYUN_ACCESS_KEY_SECRET'] = config_data.get('ALIYUN_ACCESS_KEY_SECRET', '')
    app.config['ALIYUN_CONTENT_MODERATION_REGION'] = config_data.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')
    
    app.config['ACFUN_USERNAME'] = config_data.get('ACFUN_USERNAME', '')
    app.config['ACFUN_PASSWORD'] = config_data.get('ACFUN_PASSWORD', '')

# 使用传统页面刷新方式

# 日志清理功能
def cleanup_logs(hours=168):
    """
    清理指定小时数以前的日志文件
    
    Args:
        hours: 保留最近多少小时的日志
    
    Returns:
        cleanup_stats: 清理统计信息
    """
    try:
        logger.info(f"开始清理{hours}小时前的日志文件")
        cutoff_date = datetime.now() - timedelta(hours=hours)
        cutoff_timestamp = cutoff_date.timestamp()
        
        files_removed = 0
        bytes_freed = 0
        
        # 遍历日志目录
        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            
            # 只处理文件，不处理目录
            if os.path.isfile(file_path):
                # 获取文件修改时间
                file_mtime = os.path.getmtime(file_path)
                
                # 如果文件修改时间早于截止日期，则删除
                if file_mtime < cutoff_timestamp:
                    # 获取文件大小
                    file_size = os.path.getsize(file_path)
                    
                    # 删除文件
                    os.remove(file_path)
                    
                    # 更新统计信息
                    files_removed += 1
                    bytes_freed += file_size
                    
                    logger.info(f"已删除日志文件: {filename}")
        
        # 转换字节为可读大小
        if bytes_freed < 1024:
            bytes_freed_str = f"{bytes_freed} 字节"
        elif bytes_freed < 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / 1024:.2f} KB"
        elif bytes_freed < 1024 * 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
        else:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024 * 1024):.2f} GB"
            
        logger.info(f"日志清理完成，已删除{files_removed}个文件，释放{bytes_freed_str}")
        
        return {
            "success": True,
            "files_removed": files_removed,
            "bytes_freed": bytes_freed,
            "bytes_freed_readable": bytes_freed_str,
            "cutoff_date": cutoff_date.strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"清理日志文件时出错: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def clear_specific_logs():
    """
    立即清空特定的日志文件（清空主要日志，删除任务日志）
    
    Returns:
        clear_stats: 清理统计信息
    """
    try:
        logger.info("开始清空特定日志文件")
        
        files_processed = 0
        bytes_freed = 0
        processed_files = []
        
        # 定义需要清空内容的日志文件（保留文件，只清空内容）
        clear_files = ['task_manager.log', 'app.log']
        
        # 先处理固定名称的日志文件 - 清空内容
        for filename in clear_files:
            file_path = os.path.join(log_dir, filename)
            if os.path.exists(file_path):
                try:
                    # 获取文件大小
                    file_size = os.path.getsize(file_path)
                    
                    # 清空文件内容
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write('')
                    
                    files_processed += 1
                    bytes_freed += file_size
                    processed_files.append(f"{filename} (已清空)")
                    logger.info(f"已清空日志文件: {filename}")
                except Exception as e:
                    logger.error(f"清空文件 {filename} 失败: {str(e)}")
        
        # 处理任务日志文件 (格式如: task_xxx.log) - 删除文件
        task_files_to_delete = []
        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            
            # 检查是否是任务日志文件
            if (os.path.isfile(file_path) and 
                filename.startswith('task_') and 
                filename.endswith('.log') and
                filename not in clear_files):
                task_files_to_delete.append((filename, file_path))
        
        # 删除任务日志文件
        for filename, file_path in task_files_to_delete:
            try:
                # 获取文件大小
                file_size = os.path.getsize(file_path)
                
                # 删除文件
                os.remove(file_path)
                
                files_processed += 1
                bytes_freed += file_size
                processed_files.append(f"{filename} (已删除)")
                logger.info(f"已删除任务日志文件: {filename}")
            except Exception as e:
                logger.error(f"删除文件 {filename} 失败: {str(e)}")
        
        # 转换字节为可读大小
        if bytes_freed < 1024:
            bytes_freed_str = f"{bytes_freed} 字节"
        elif bytes_freed < 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / 1024:.2f} KB"
        elif bytes_freed < 1024 * 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
        else:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024 * 1024):.2f} GB"
            
        logger.info(f"日志清理完成，已处理{files_processed}个文件，释放{bytes_freed_str}")
        
        return {
            "success": True,
            "files_processed": files_processed,
            "bytes_freed": bytes_freed,
            "bytes_freed_readable": bytes_freed_str,
            "processed_files": processed_files
        }
    except Exception as e:
        logger.error(f"清理日志文件时出错: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def auto_start_pending_tasks(config):
    """自动启动所有pending状态的任务"""
    try:
        from modules.task_manager import get_all_tasks, start_task, TASK_STATES
        
        # 获取所有pending任务
        all_tasks = get_all_tasks()
        pending_tasks = [task for task in all_tasks if task['status'] == TASK_STATES['PENDING']]
        
        if not pending_tasks:
            logger.info("没有pending状态的任务需要启动")
            return
        
        logger.info(f"发现 {len(pending_tasks)} 个pending任务，正在启动...")
        
        started_count = 0
        for task in pending_tasks:
            try:
                success = start_task(task['id'], config)
                if success:
                    started_count += 1
                    logger.info(f"已启动任务: {task['id'][:8]}... ({task.get('youtube_url', 'Unknown URL')[-30:]})")
                else:
                    logger.warning(f"启动任务失败: {task['id'][:8]}...")
            except Exception as e:
                logger.error(f"启动任务 {task['id'][:8]}... 时出错: {str(e)}")
        
        logger.info(f"自动启动完成，成功启动了 {started_count}/{len(pending_tasks)} 个任务")
        
    except Exception as e:
        logger.error(f"自动启动pending任务时出错: {str(e)}")

# 下载内容清理功能
def cleanup_downloads(hours=720):
    """
    清理指定小时数以前的下载内容
    
    Args:
        hours: 保留最近多少小时的下载内容
    
    Returns:
        cleanup_stats: 清理统计信息
    """
    try:
        logger.info(f"开始清理{hours}小时前的下载内容")
        cutoff_date = datetime.now() - timedelta(hours=hours)
        cutoff_timestamp = cutoff_date.timestamp()
        
        files_removed = 0
        dirs_removed = 0
        bytes_freed = 0
        downloads_dir = get_app_subdir('downloads')
        
        # 遍历下载目录
        for item_name in os.listdir(downloads_dir):
            item_path = os.path.join(downloads_dir, item_name)
            
            # 检查是否是目录（通常每个任务一个目录）
            if os.path.isdir(item_path):
                # 获取目录修改时间
                dir_mtime = os.path.getmtime(item_path)
                
                # 如果目录修改时间早于截止日期，则删除整个目录及其内容
                if dir_mtime < cutoff_timestamp:
                    # 计算目录大小
                    dir_size = 0
                    file_count = 0
                    for root, dirs, files in os.walk(item_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            try:
                                dir_size += os.path.getsize(file_path)
                                file_count += 1
                            except (OSError, IOError):
                                pass  # 忽略无法访问的文件
                    
                    # 删除目录
                    shutil.rmtree(item_path)
                    
                    # 更新统计信息
                    dirs_removed += 1
                    files_removed += file_count
                    bytes_freed += dir_size
                    
                    logger.info(f"已删除下载目录: {item_name} (包含{file_count}个文件)")
            elif os.path.isfile(item_path):
                # 处理单个文件
                file_mtime = os.path.getmtime(item_path)
                if file_mtime < cutoff_timestamp:
                    # 获取文件大小
                    file_size = os.path.getsize(item_path)
                    
                    # 删除文件
                    os.remove(item_path)
                    
                    # 更新统计信息
                    files_removed += 1
                    bytes_freed += file_size
                    
                    logger.info(f"已删除下载文件: {item_name}")
        
        # 转换字节为可读大小
        if bytes_freed < 1024:
            bytes_freed_str = f"{bytes_freed} 字节"
        elif bytes_freed < 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / 1024:.2f} KB"
        elif bytes_freed < 1024 * 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
        else:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024 * 1024):.2f} GB"
            
        logger.info(f"下载内容清理完成，已删除{dirs_removed}个目录、{files_removed}个文件，释放{bytes_freed_str}")
        
        return {
            "success": True,
            "dirs_removed": dirs_removed,
            "files_removed": files_removed,
            "bytes_freed": bytes_freed,
            "bytes_freed_readable": bytes_freed_str,
            "cutoff_date": cutoff_date.strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"清理下载内容时出错: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def schedule_log_cleanup():
    """根据配置设置日志清理定时任务"""
    config = load_config()
    
    if config.get('LOG_CLEANUP_ENABLED', False):
        hours = int(config.get('LOG_CLEANUP_HOURS', 168))  # 默认保留7天=168小时
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 24))
        
        # 创建调度器
        scheduler = BackgroundScheduler()
        
        # 添加定时任务，每隔指定小时执行一次
        scheduler.add_job(
            cleanup_logs,
            'interval',
            hours=interval_hours,
            kwargs={'hours': hours},
            id='log_cleanup_job'
        )
        
        # 启动调度器
        scheduler.start()
        
        logger.info(f"已启用日志自动清理，保留{hours}小时内的日志，每{interval_hours}小时清理一次")
        return scheduler
    else:
        logger.info("日志自动清理已禁用")
        return None

def schedule_download_cleanup():
    """根据配置设置下载内容清理定时任务"""
    try:
        config = load_config()
        
        if config.get('DOWNLOAD_CLEANUP_ENABLED', False):
            hours = int(config.get('DOWNLOAD_CLEANUP_HOURS', 720))  # 默认保留30天=720小时
            interval_hours = int(config.get('DOWNLOAD_CLEANUP_INTERVAL', 24))
            
            # 创建调度器
            scheduler = BackgroundScheduler()
            
            # 添加定时任务，每隔指定小时执行一次
            scheduler.add_job(
                cleanup_downloads,
                'interval',
                hours=interval_hours,
                kwargs={'hours': hours},
                id='download_cleanup_job'
            )
            
            # 启动调度器
            scheduler.start()
            
            logger.info(f"已启用下载内容自动清理，保留{hours}小时内的下载内容，每{interval_hours}小时清理一次")
            return scheduler
        else:
            logger.info("下载内容自动清理已禁用")
            return None
    except Exception as e:
        logger.error(f"设置下载内容清理定时任务时出错: {str(e)}")
        return None

@app.route('/maintenance/cleanup_logs', methods=['POST'])
@login_required
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('LOG_CLEANUP_HOURS', 168)))
    
    result = cleanup_logs(hours)
    
    if result.get('success'):
        flash(f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/clear_logs', methods=['POST'])
@login_required
def clear_logs_route():
    """立即清空特定日志文件"""
    result = clear_specific_logs()
    
    if result.get('success'):
        processed_files_str = "、".join(result['processed_files'])
        flash(f"日志清理成功，已处理{result['files_processed']}个文件（{processed_files_str}），释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/cleanup_downloads', methods=['POST'])
@login_required
def cleanup_downloads_route():
    """手动触发下载内容清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('DOWNLOAD_CLEANUP_HOURS', 720)))
    
    result = cleanup_downloads(hours)
    
    if result.get('success'):
        flash(f"下载内容清理成功，删除了{result['dirs_removed']}个目录、{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"下载内容清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

# YouTube监控系统路由
@app.route('/youtube_monitor')
@login_required
def youtube_monitor_index():
    """YouTube监控主页"""
    configs = youtube_monitor.get_monitor_configs()
    history = youtube_monitor.get_monitor_history(limit=50)
    return render_template('youtube_monitor.html', configs=configs, history=history)

@app.route('/youtube_monitor/config', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config():
    """监控配置页面"""
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html')
            
            config_id = youtube_monitor.create_monitor_config(config_data)
            flash(f'监控配置 "{config_data["name"]}" 创建成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'创建监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html')

@app.route('/youtube_monitor/config/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config_edit(config_id):
    """编辑监控配置"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html', config=config, is_edit=True)
            
            youtube_monitor.update_monitor_config(config_id, config_data)
            flash(f'监控配置更新成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'更新监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html', config=config, is_edit=True)

@app.route('/youtube_monitor/config/<int:config_id>/delete', methods=['POST'])
@login_required
def youtube_monitor_config_delete(config_id):
    """删除监控配置"""
    try:
        config = youtube_monitor.get_monitor_config(config_id)
        if config:
            youtube_monitor.delete_monitor_config(config_id)
            flash(f'监控配置 "{config["name"]}" 删除成功！', 'success')
        else:
            flash('监控配置不存在', 'danger')
    except Exception as e:
        flash(f'删除监控配置失败: {str(e)}', 'danger')
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/run', methods=['POST'])
@login_required
def youtube_monitor_run(config_id):
    """立即执行一次监控任务"""
    success, message = youtube_monitor.run_monitor(config_id)
    if success:
        flash(message or '监控已执行', 'success')
    else:
        flash(message or '监控执行失败', 'danger')
    return redirect(url_for('youtube_monitor_history', config_id=config_id))

@app.route('/youtube_monitor/history/<int:config_id>')
@login_required
def youtube_monitor_history(config_id):
    """查看指定监控配置的发现历史"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    history = youtube_monitor.get_monitor_history(config_id, limit=200)
    
    # 计算统计数据
    stats = {
        'total_records': len(history),
        'added_to_tasks': 0,
        'avg_views': 0,
        'avg_likes': 0
    }
    
    if history:
        total_views = 0
        total_likes = 0
        
        for record in history:
            if record.get('added_to_tasks'):
                stats['added_to_tasks'] += 1
            total_views += record.get('view_count', 0)
            total_likes += record.get('like_count', 0)
        
        stats['avg_views'] = int(total_views / len(history))
        stats['avg_likes'] = int(total_likes / len(history))
    
    return render_template('youtube_monitor_history.html', history=history, config=config, stats=stats)

@app.route('/youtube_monitor/add_to_tasks', methods=['POST'])
@login_required
def youtube_monitor_add_to_tasks():
    """从监控历史中添加视频到任务列表"""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    config_id = data.get('config_id')
    if not video_id or not config_id:
        return jsonify({'success': False, 'message': '参数不完整'}), 400
    try:
        config_id_int = int(config_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'config_id 无效'}), 400

    success, message = youtube_monitor.add_video_to_tasks_manually(video_id, config_id_int)

    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 400

@app.route('/youtube_monitor/history/<int:config_id>/clear', methods=['POST'])
@login_required
def youtube_monitor_clear_history(config_id):
    """清空指定监控任务的历史记录"""
    youtube_monitor.clear_monitor_history(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/history/clear_all', methods=['POST'])
@login_required
def youtube_monitor_clear_all_history():
    """清空所有历史记录"""
    youtube_monitor.clear_all_monitor_history()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/restore_configs', methods=['POST'])
@login_required
def youtube_monitor_restore_configs():
    """恢复默认监控配置"""
    youtube_monitor.restore_configs_from_files_manually()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/reset_offset', methods=['POST'])
@login_required
def youtube_monitor_reset_offset(config_id):
    """重置频道监控的视频偏移量"""
    youtube_monitor.reset_historical_offset(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/api/cookies/sync', methods=['POST'])
@login_required
def sync_cookies():
    """
    接收从浏览器扩展同步过来的Cookie
    """
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须是JSON格式'}), 400
        
        data = request.get_json()
        
        # 验证必要字段
        required_fields = ['source', 'timestamp', 'cookies', 'cookieCount']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'缺少必要字段: {field}'}), 400
        
        # 验证来源
        if data['source'] not in ['userscript', 'extension']:
            return jsonify({'error': '不支持的cookie来源'}), 400
        
        # 验证cookie数据
        cookies_content = data['cookies']
        if not cookies_content or not isinstance(cookies_content, str):
            return jsonify({'error': 'cookie数据无效'}), 400
        
        # 保存cookie到文件
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        # 不再创建备份文件（用户要求禁用备份功能）
        # if data['source'] == 'userscript' and os.path.exists(youtube_cookies_path):
        #     backup_path = youtube_cookies_path + f'.backup.{int(time.time())}'
        #     try:
        #         shutil.copy2(youtube_cookies_path, backup_path)
        #         logger.info(f"已备份原有cookie文件到: {backup_path}")
        #     except Exception as e:
        #         logger.warning(f"备份cookie文件失败: {str(e)}")
        
        # 写入新的cookie文件
        try:
            with open(youtube_cookies_path, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            
            # 记录同步信息
            sync_info = {
                'timestamp': data['timestamp'],
                'sync_time': time.time(),
                'cookie_count': data['cookieCount'],
                'user_agent': data.get('userAgent', ''),
                'source_url': data.get('url', ''),
                'file_size': len(cookies_content)
            }
            
            source_name = '浏览器扩展' if data['source'] == 'extension' else '油猴脚本'
            logger.info(f"Cookie同步成功 - 来源: {source_name}, 数量: {data['cookieCount']}, 大小: {len(cookies_content)} bytes")
            
            # 备份文件功能已禁用，不再需要清理逻辑
            # try:
            #     backup_files = []
            #     for file in os.listdir(cookies_dir):
            #         if file.startswith('yt_cookies.txt.backup.'):
            #             backup_path = os.path.join(cookies_dir, file)
            #             backup_files.append((os.path.getmtime(backup_path), backup_path))
            #     
            #     # 按时间排序，删除多余的备份
            #     if len(backup_files) > 5:
            #         backup_files.sort()
            #         for _, old_backup in backup_files[:-5]:
            #             try:
            #                 os.remove(old_backup)
            #                 logger.debug(f"已删除旧备份文件: {old_backup}")
            #             except Exception as e:
            #                 logger.warning(f"删除旧备份文件失败: {str(e)}")
            # except Exception as e:
            #     logger.warning(f"清理备份文件失败: {str(e)}")
            
            return jsonify({
                'success': True,
                'message': 'Cookie同步成功',
                'sync_info': sync_info
            }), 200
            
        except Exception as e:
            logger.error(f"写入cookie文件失败: {str(e)}")
            return jsonify({'error': f'保存cookie失败: {str(e)}'}), 500
            
    except Exception as e:
        logger.error(f"Cookie同步API异常: {str(e)}")
        return jsonify({'error': f'服务器内部错误: {str(e)}'}), 500

@app.route('/api/cookies/status', methods=['GET'])
@login_required
def get_cookie_status():
    """
    提供Cookie状态给浏览器扩展
    """
    try:
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        status = {
            'youtube_cookies_exists': os.path.exists(youtube_cookies_path),
            'last_modified': None,
            'file_size': 0,
            'line_count': 0
        }
        
        if status['youtube_cookies_exists']:
            stat_info = os.stat(youtube_cookies_path)
            status['last_modified'] = stat_info.st_mtime
            status['file_size'] = stat_info.st_size
            
            # 统计行数
            try:
                with open(youtube_cookies_path, 'r', encoding='utf-8') as f:
                    status['line_count'] = sum(1 for line in f if line.strip() and not line.startswith('#'))
            except Exception as e:
                logger.warning(f"读取cookie文件失败: {str(e)}")
                status['line_count'] = -1
        
        return jsonify(status), 200
        
    except Exception as e:
        logger.error(f"获取cookie状态失败: {str(e)}")
        return jsonify({'error': f'获取状态失败: {str(e)}'}), 500

@app.route('/api/cookies/refresh-needed', methods=['POST'])
@login_required
def cookie_refresh_needed():
    """
    接收浏览器扩展的通知，标记某个网站的Cookie需要刷新
    """
    try:
        data = request.get_json()
        reason = data.get('reason', 'unknown')
        video_url = data.get('video_url', '')
        
        logger.warning(f"收到Cookie刷新需求 - 原因: {reason}, 视频: {video_url}")
        
        # 这里可以实现通知机制，比如：
        # 1. 发送到浏览器扩展
        # 2. 在Web界面显示提示
        # 3. 发送邮件通知等
        
        return jsonify({
            'success': True,
            'message': 'Cookie刷新需求已记录',
            'suggestion': '请使用浏览器扩展重新同步Cookie'
        }), 200
        
    except Exception as e:
        logger.error(f"处理Cookie刷新需求失败: {str(e)}")
        return jsonify({'error': f'处理失败: {str(e)}'}), 500

if __name__ == '__main__':
    logger.info("Y2A-Auto 启动中...")
    
    # 初始化AcFun分区ID映射
    init_id_mapping()
    
    # 加载配置
    config = load_config()
    app.config['Y2A_SETTINGS'] = config
    logger.info(f"配置已加载: {json.dumps(config, ensure_ascii=False, indent=2)}")
    
    # 初始化全局任务处理器，确保并发控制生效
    from modules.task_manager import get_global_task_processor, shutdown_global_task_processor
    get_global_task_processor(config)
    logger.info("全局任务处理器已初始化")
    
    # 自动启动所有pending任务（如果启用了自动模式）
    if config.get('AUTO_MODE_ENABLED', False):
        logger.info("自动模式已启用，正在启动所有pending任务...")
        auto_start_pending_tasks(config)
    
    # 初始化YouTube监控API
    if config.get('YOUTUBE_API_KEY'):
        youtube_monitor.set_api_key(config['YOUTUBE_API_KEY'])
        youtube_monitor.start_all_schedules()
        logger.info("YouTube监控系统已初始化")
    
    # 配置应用
    configure_app(app, config)
    
    # 设置日志清理定时任务
    log_cleanup_scheduler = schedule_log_cleanup()
    
    # 设置下载内容清理定时任务
    download_cleanup_scheduler = schedule_download_cleanup()
    
    try:
        logger.info(f"服务启动，监听地址: http://127.0.0.1:{5000}")
        # 使用标准Flask运行
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        # 关闭全局任务处理器
        shutdown_global_task_processor()
        
        if log_cleanup_scheduler:
            log_cleanup_scheduler.shutdown()
        if download_cleanup_scheduler:
            download_cleanup_scheduler.shutdown()
        logger.info("服务已关闭") 