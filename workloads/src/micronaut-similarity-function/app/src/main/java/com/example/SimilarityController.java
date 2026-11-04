/*
 * Copyright 2020-2021 the original author or authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.example;

import io.micronaut.http.HttpResponse;
import io.micronaut.http.MediaType;
import io.micronaut.http.annotation.Body;
import io.micronaut.http.annotation.Controller;
import io.micronaut.http.annotation.Get;
import io.micronaut.http.annotation.Post;
import io.micronaut.http.annotation.Produces;
import io.micronaut.serde.annotation.Serdeable;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Serdeable
class Request {
  String[] texts;

  Request(String[] texts) {
    this.texts = texts;
  }
}

@Controller("/similarity")
public class SimilarityController {

  private static final String[] SMOKE_TEST_TEXTS = {
      readResource("workloads/essay1.txt"),
      readResource("workloads/essay2.txt")
  };

  @Post(value = "/order", consumes = MediaType.APPLICATION_JSON)
  @Produces(MediaType.APPLICATION_JSON)
  public HttpResponse<Map<String, Object>> get(@Body Request req) {
    if (req.texts == null) {
      Map<String, Object> response = new HashMap<>();
      response.put("error", "Request has no 'texts' field!");
      return HttpResponse.badRequest(response);
    }
    return calculate(req.texts);
  }

  private HttpResponse<Map<String, Object>> calculate(String[] texts) {
    var start = System.nanoTime();
    var profiles = SimilarityMetrics.profiles(texts);
    List<String> qgram = SimilarityMetrics.pairsByQgram(profiles);
    List<String> cosine = SimilarityMetrics.pairsByCosine(profiles);
    List<String> sorensenDice = SimilarityMetrics.pairsBySorensenDice(profiles);
    List<String> jaccard = SimilarityMetrics.pairsByJaccard(profiles);
    List<String> tversky = SimilarityMetrics.pairsByTversky(profiles);
    List<String> szymkiewiczSimpson = SimilarityMetrics.pairsBySzymkiewiczSimpson(profiles);
    var time = System.nanoTime() - start;

    Map<String, Object> response = new HashMap<>();
    response.put("qgram", qgram);
    response.put("cosine", cosine);
    response.put("sorensenDice", sorensenDice);
    response.put("jaccard", jaccard);
    response.put("tversky", tversky);
    response.put("szymkiewiczSimpson", szymkiewiczSimpson);
    response.put("time", time);
    return HttpResponse.ok(response);
  }

    @Get("/smoke-test")
    @Produces(MediaType.APPLICATION_JSON)
    public HttpResponse<Map<String, Object>> smokeTest() {
        return calculate(SMOKE_TEST_TEXTS);
    }

    @Get
    @Produces(MediaType.APPLICATION_JSON)
    public HttpResponse<Map<String, Object>> get() {
        return HttpResponse.ok(new HashMap<>());
    }

    private static String readResource(String resourceName) {
        try (var stream = SimilarityController.class.getClassLoader().getResourceAsStream(resourceName)) {
            if (stream == null) {
                throw new IllegalStateException("Missing smoke-test resource: " + resourceName);
            }
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException("Could not read smoke-test resource: " + resourceName, e);
        }
    }
}
