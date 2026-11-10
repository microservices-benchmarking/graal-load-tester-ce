package com.example.webmvc;

import rawhttp.core.RawHttp;
import rawhttp.core.RawHttpRequest;
import rawhttp.core.errors.InvalidHttpHeader;
import rawhttp.core.errors.InvalidHttpRequest;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.channels.Channel;
import java.nio.channels.Channels;
import java.nio.channels.ReadableByteChannel;
import java.nio.channels.WritableByteChannel;
import java.nio.charset.StandardCharsets;

public final class SpringHelloWorldFunction {
    private static final WebmvcController CONTROLLER = new WebmvcController();

    private SpringHelloWorldFunction() {
    }

    public static void main(String[] args) throws Exception {
        if (Boolean.getBoolean("FAAS_COMPATIBLE")) {
            Channel channel = System.inheritedChannel();
            try (InputStream in = Channels.newInputStream((ReadableByteChannel) channel);
                 OutputStream out = Channels.newOutputStream((WritableByteChannel) channel)) {
                run(in, out);
            }
        } else {
            run(System.in, System.out);
        }
    }

    static void run(InputStream inputStream, OutputStream outputStream) throws IOException {
        RawHttp rawHttp = new RawHttp();
        while (true) {
            try {
                RawHttpRequest request = rawHttp.parseRequest(inputStream);
                writeResponse(outputStream, route(request));
            } catch (InvalidHttpRequest | InvalidHttpHeader e) {
                break;
            } catch (Exception e) {
                writeResponse(outputStream, 500, "text/plain", "spring-hello-world-function failed: " + e.getMessage() + "\n");
            }
        }
    }

    private static Response route(RawHttpRequest request) {
        String path = request.getUri().getPath();
        if ("GET".equals(request.getMethod()) && "/hello".equals(path)) {
            return new Response(200, "text/plain", CONTROLLER.hello());
        }
        return new Response(404, "text/plain", "No route for " + request.getMethod() + " " + path + "\n");
    }

    private static void writeResponse(OutputStream outputStream, Response response) throws IOException {
        writeResponse(outputStream, response.statusCode(), response.contentType(), response.body());
    }

    private static void writeResponse(OutputStream outputStream, int statusCode, String contentType, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        String reason = statusCode == 200 ? "OK" : statusCode == 404 ? "Not Found" : "Internal Server Error";
        String headers = "HTTP/1.1 " + statusCode + " " + reason + "\r\n"
            + "Content-Type: " + contentType + "\r\n"
            + "Content-Length: " + bytes.length + "\r\n"
            + "Connection: keep-alive\r\n\r\n";
        outputStream.write(headers.getBytes(StandardCharsets.US_ASCII));
        outputStream.write(bytes);
        outputStream.flush();
    }

    private record Response(int statusCode, String contentType, String body) {
    }
}
