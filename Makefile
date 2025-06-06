# Y2A-Auto Docker 管理工具

.PHONY: help build up down logs restart clean build-local

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
	@echo "  make build       - 本地构建镜像"
	@echo "  make build-local - 使用本地构建配置启动"
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