FROM python:3.14-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git cmake ninja-build build-essential ffmpeg yasm \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Clone and build AVM (aomenc for AV2)
RUN git clone https://github.com/AOMediaCodec/avm.git && \
    cd avm && git checkout 63be7278689dcff55a60fa94e02feb4a67daaa8b && \
    mkdir -p build && cd build && \
    cmake -G Ninja -DCMAKE_BUILD_TYPE=Release .. && \
    ninja avmenc

# Clone and build av2-tools (feature/multi_cvs branch)
RUN git clone https://github.com/AOMediaCodec/av2-tools.git && \
    cd av2-tools && git checkout bf18846d47438a66f60eb62ba8c868251a705151 && \
    mkdir -p build && cd build && \
    cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_CONTAINER_TOOLS=ON .. && \
    ninja av2_mux

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY server.py .
COPY templates/ templates/

# Create folders for uploads and outputs
RUN mkdir -p uploads outputs

EXPOSE 8080

CMD ["python", "server.py"]
