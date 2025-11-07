# Y2A-Auto Docker 管理工具

.PHONY: help build up down logs restart clean build-local build-legacy build-local-legacy

# 默认目标
help:
	@echo "Y2A-Auto Docker 管理命令:"
	@echo ""
	@echo "生产环境:"
	@echo "  make up          - 启动应用 (使用预构建镜像)"
	@echo "  make down        - 停止应用"
	@echo "  make logs        - 查看日志"
	@echo "  make restart     - 重启应用"
	@echo ""
	@echo "构建相关:"
	@echo "  make build                - 本地构建镜像 (默认，使用 Dockerfile)"
	@echo "  make build-local          - 使用本地构建配置启动 (默认)"
	@echo "  make build-legacy         - 兼容无 BuildKit 的构建 (使用 Dockerfile.nobuildkit)"
	@echo "  make build-local-legacy   - 兼容无 BuildKit 的构建并启动"
	@echo ""
	@echo "健康检查和诊断:"
	@echo "  make health      - 基础健康检查"
	@echo "  make health-check - 详细系统健康检查"
	@echo "  make diagnose    - 快速环境诊断"
	@echo "  make status      - 查看容器状态"
	@echo ""
	@echo "问题修复:"
	@echo "  make fix-permissions - 修复文件权限问题"
	@echo "  make reset-docker    - 重置 Docker 环境 (危险操作)"
	@echo ""
	@echo "维护清理:"
	@echo "  make clean       - 清理 Docker 资源"
	@echo "  make clean-all   - 深度清理 (包括卷和网络)"

# 生产环境命令
up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

restart:
	docker-compose restart

# 构建相关命令
build:
	docker-compose -f docker-compose-build.yml build

build-local:
	docker-compose -f docker-compose-build.yml up -d

# 兼容无 BuildKit 的构建
build-legacy:
	docker-compose -f docker-compose-build-legacy.yml build

build-local-legacy:
	docker-compose -f docker-compose-build-legacy.yml up -d

# 清理命令
clean:
	docker system prune -f
	docker image prune -f

clean-all:
	docker-compose down -v
	docker-compose -f docker-compose-build.yml down -v
	docker system prune -af
	docker volume prune -f
	docker network prune -f

# 健康检查
health:
	docker-compose ps
	@echo ""
	@echo "健康状态检查:"
	@curl -s http://localhost:5000/ > /dev/null && echo "✅ 应用运行正常" || echo "❌ 应用无法访问"

# 查看容器状态
status:
	docker-compose ps
	@echo ""
	@echo "容器资源使用情况:"
	@docker stats --no-stream y2a-auto 2>/dev/null || echo "容器未运行"

# 系统健康检查（新增）
health-check:
	@echo "=== Y2A-Auto 系统健康检查 ==="
	@echo ""
	@echo "1. 容器状态:"
	@docker-compose ps
	@echo ""
	@echo "2. 应用健康检查:"
	@curl -s http://localhost:5000/system_health | python -m json.tool 2>/dev/null || echo "❌ 健康检查API无法访问"
	@echo ""
	@echo "3. 挂载目录检查:"
	@docker-compose exec y2a-auto ls -la /app/config /app/db /app/cookies 2>/dev/null || echo "❌ 无法访问容器目录"

# 简化诊断
diagnose:
	@echo "=== Y2A-Auto 环境诊断 ==="
	@echo ""
	@echo "容器状态:"
	@docker ps --filter "name=y2a-auto" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "容器未运行"
	@echo ""
	@echo "最近日志:"
	@docker-compose logs --tail=5 y2a-auto 2>/dev/null || echo "无法获取日志"

# 修复常见问题
fix-permissions:
	@echo "修复文件权限问题..."
	@sudo chown -R $$USER:$$USER ./config ./db ./downloads ./logs ./cookies ./temp 2>/dev/null || echo "权限修复完成"
	@chmod -R 755 ./config ./db ./downloads ./logs ./cookies ./temp 2>/dev/null || echo "目录权限设置完成"

# 重置 Docker 环境
reset-docker:
	@echo "⚠️  警告: 这将删除所有容器、卷和网络！"
	@read -p "确认重置? (y/N): " confirm && [ "$$confirm" = "y" ] || exit 1
	@echo "正在重置 Docker 环境..."
	@docker-compose down -v
	@docker system prune -f
	@echo "✅ Docker 环境已重置" 