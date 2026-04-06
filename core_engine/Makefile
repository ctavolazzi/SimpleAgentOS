
setup:
	cd frontend && npm install
	pip install fastapi uvicorn httpx

dev:
	(sleep 3 && open http://localhost:5173) &
	npx concurrently \
		"~/Code/llama.cpp/build/bin/llama-server -m /Users/ctavolazzi/google_gemma-4-E4B-it-Q4_K_M.gguf --port 8080" \
		"./pocketbase serve" \
		"python3 nerve_center.py" \
		"cd frontend && npm run dev" \
		--names "BRAIN,DB,RELAY,HUD" \
		--prefix_colors "magenta,yellow,green,cyan"
