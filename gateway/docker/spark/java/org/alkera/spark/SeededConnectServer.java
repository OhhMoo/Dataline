package org.alkera.spark;

import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;
import org.apache.spark.sql.connect.service.SparkConnectService$;

/**
 * Spark's own Spark Connect server main ({@code SparkConnectServer}), with the world seeded
 * into the session catalog BEFORE the gRPC port is bound.
 *
 * <pre>
 *   spark-submit --class org.alkera.spark.SeededConnectServer spark-internal SEED_SQL EXPECT_TSV
 * </pre>
 *
 * <p>{@code SEED_SQL} is one statement per line (gateway/docker/spark/spark_start.py writes
 * it: CREATE SCHEMA, then CREATE TABLE ... USING DELTA AS SELECT ... FROM parquet.`...`).
 * {@code EXPECT_TSV} is {@code <table>\t<rows>} per line, the world's own counts: every
 * table is counted back after seeding, and a table whose count differs stops the process
 * (exit 3) before anything listens - a partial seed is not a seed. One JSON line on
 * stdout says what was seeded and that the port is bound; nothing else is printed there.
 *
 * <p>The catalog is in-memory (no metastore), so the seed has to run in the server's own
 * JVM: a seeding process before it would leave nothing the server could see. One JVM is
 * also one cold start instead of two.
 */
public final class SeededConnectServer {
  private SeededConnectServer() {}

  public static void main(String[] args) throws Exception {
    if (args.length != 2) {
      System.err.println("usage: SeededConnectServer SEED_SQL EXPECT_TSV");
      System.exit(2);
    }
    PrintStream out = new PrintStream(System.out, true, StandardCharsets.UTF_8);
    long started = System.nanoTime();
    SparkSession spark = SparkSession.builder().getOrCreate();
    for (String statement : Files.readAllLines(Path.of(args[0]), StandardCharsets.UTF_8)) {
      if (!statement.isBlank()) {
        spark.sql(statement).collect();
      }
    }
    List<String> seeded = new ArrayList<>();
    for (String line : Files.readAllLines(Path.of(args[1]), StandardCharsets.UTF_8)) {
      if (line.isBlank()) {
        continue;
      }
      String[] cut = line.split("\t", 2);
      Row[] got = (Row[]) spark.sql("SELECT count(*) FROM " + cut[0]).collect();
      long rows = got[0].getLong(0);
      if (rows != Long.parseLong(cut[1])) {
        System.err.println("seed: " + cut[0] + " has " + rows + " rows, the world has " + cut[1]);
        System.exit(3);
      }
      seeded.add("{\"table\":\"" + cut[0] + "\",\"rows\":" + rows + "}");
    }
    long seededMs = (System.nanoTime() - started) / 1_000_000;
    Guard.arm(spark.conf().get("spark.sql.warehouse.dir"));
    SparkConnectService$.MODULE$.start(spark.sparkContext());
    long listeningMs = (System.nanoTime() - started) / 1_000_000;
    out.println("{\"spark_connect\":\"listening\",\"port\":"
        + SparkConnectService$.MODULE$.bindingAddress().getPort()
        + ",\"spark\":\"" + spark.version() + "\",\"seed_ms\":" + seededMs
        + ",\"listening_ms\":" + listeningMs + ",\"seed\":[" + String.join(",", seeded) + "]}");
    SparkConnectService$.MODULE$.server().awaitTermination();
  }
}
