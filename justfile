default: dev

# Run dev server with CSS watching
dev:
    tailwindcss -i static/input.css -o static/tailwind.css --watch &
    uv run quart --app main run --reload

# Build CSS for production
build-css:
    tailwindcss -i static/input.css -o static/tailwind.css --minify

# Build Docker image
build:
    docker build -t morsels .

# Run with Docker Compose
up:
    docker compose up -d

# View logs
logs:
    docker compose logs -f
