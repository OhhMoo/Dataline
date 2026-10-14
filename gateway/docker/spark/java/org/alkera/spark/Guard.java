package org.alkera.spark;

import java.lang.reflect.Method;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.Set;
import org.apache.spark.sql.SparkSessionExtensions;
import org.apache.spark.sql.catalyst.expressions.Expression;
import org.apache.spark.sql.catalyst.plans.logical.LogicalPlan;
import scala.jdk.javaapi.CollectionConverters;
import scala.runtime.BoxedUnit;

/**
 * The engine's own fences, a check rule over every analyzed plan (spark.sql.extensions).
 *
 * <p>The audited proxy in native-services refuses whole RPCs and plan types by name before
 * anything reaches this engine. This rule is the second fence, for what only the analyzer
 * can see whatever the client sent - SQL text, a {@code F.expr(...)}, a DataFrame call: the
 * engine sits on backend_net beside the stack's own services, so nothing the agent sends
 * may run a command, load code or reach a file or a URL from here. Each refusal names the
 * feature ({@link #NOT_AVAILABLE}) and the plan never runs:
 *
 * <ul>
 *   <li>TRANSFORM (script transformation runs a shell command), ADD JAR/FILE/ARCHIVE and
 *       CREATE FUNCTION (load code), reflect/java_method (call a JVM method), Python
 *       user-defined functions;
 *   <li>a table or a read that is a file PATH or URL rather than a catalog table - {@code
 *       parquet.`/x`}, {@code spark.read.load(path)}, LOCATION, LOAD DATA, INSERT OVERWRITE
 *       DIRECTORY - and streaming: DBFS and cloud paths are not provided, tables are;
 *   <li>SET of a configuration that changes what code runs or where data comes from
 *       ({@link #FENCED_CONFS}).
 * </ul>
 *
 * <p>Armed by {@link SeededConnectServer} after the seed, which reads the world's Parquet
 * files by path - the one thing the agent may not do.
 */
public final class Guard implements scala.Function1<SparkSessionExtensions, BoxedUnit> {
  public static final String NOT_AVAILABLE = "Spark feature not available in this environment";
  static volatile boolean armed = false;
  /**
   * The managed tables' directory (spark.sql.warehouse.dir, as a path). A relation over
   * files below it is a catalog table's own (Delta replays its _delta_log by path), and
   * reading it by path shows nothing its table does not.
   */
  static volatile String warehouse = null;

  /** Fences on, for every plan analyzed from now on; `warehousePath` as in the conf. */
  public static void arm(String warehousePath) {
    warehouse = pathOf(warehousePath);
    armed = true;
  }

  static String pathOf(String location) {
    String path = java.net.URI.create(location.contains(":") ? location : "file:" + location)
        .getPath();
    return path.endsWith("/") ? path : path + "/";
  }

  /** Logical plan nodes refused wherever they appear, by class name. */
  static final Map<String, String> FENCED_NODES = Map.ofEntries(
      Map.entry("ScriptTransformation", "TRANSFORM (script transformation runs a command)"),
      Map.entry("AddJarsCommand", "ADD JAR (loads code)"),
      Map.entry("AddFilesCommand", "ADD FILE"),
      Map.entry("AddArchivesCommand", "ADD ARCHIVE"),
      Map.entry("CreateFunctionCommand", "CREATE FUNCTION"),
      Map.entry("CreateFunction", "CREATE FUNCTION"),
      Map.entry("CreateUserDefinedFunctionCommand", "CREATE FUNCTION"),
      Map.entry("CreateSQLFunctionCommand", "CREATE FUNCTION"),
      Map.entry("LoadDataCommand", "LOAD DATA (reads a file path)"),
      Map.entry("InsertIntoDataSourceDirCommand", "INSERT OVERWRITE DIRECTORY (writes a file path)"),
      Map.entry("InsertIntoHiveDirCommand", "INSERT OVERWRITE DIRECTORY (writes a file path)"),
      Map.entry("CreateTempViewUsing", "CREATE TEMPORARY VIEW ... USING (a data source by path)"),
      Map.entry("AlterTableSetLocationCommand", "ALTER TABLE ... SET LOCATION (a file path)"),
      Map.entry("SetTableLocation", "ALTER TABLE ... SET LOCATION (a file path)"),
      Map.entry("StreamingRelation", "Structured Streaming"),
      Map.entry("StreamingRelationV2", "Structured Streaming"),
      Map.entry("StreamingExecutionRelation", "Structured Streaming"),
      Map.entry("MapInPandas", "Python UDFs (mapInPandas)"),
      Map.entry("MapInArrow", "Python UDFs (mapInArrow)"),
      Map.entry("FlatMapGroupsInPandas", "Python UDFs (applyInPandas)"),
      Map.entry("FlatMapGroupsInArrow", "Python UDFs (applyInArrow)"),
      Map.entry("FlatMapCoGroupsInPandas", "Python UDFs (cogroup applyInPandas)"),
      Map.entry("FlatMapCoGroupsInArrow", "Python UDFs (cogroup applyInArrow)"),
      Map.entry("FlatMapGroupsInPandasWithState", "Python UDFs (applyInPandasWithState)"),
      Map.entry("TransformWithStateInPandas", "Python UDFs (transformWithStateInPandas)"),
      Map.entry("BatchEvalPython", "Python UDFs"),
      Map.entry("ArrowEvalPython", "Python UDFs"));

  /**
   * Expressions refused wherever they appear, by class name.
   *
   * <p>NOT here, measured: JVM closures and Scala UDFs (ScalaUDF, MapPartitions, MapElements,
   * ...). Delta builds its own plans with them - reading a table's rows ran a ScalaUDF - and
   * this rule sees every plan the engine analyzes, Delta's included. A client cannot send
   * one except as a CommonInlineUserDefinedFunction, which the proxy refuses by name.
   */
  static final Map<String, String> FENCED_EXPRESSIONS = Map.of(
      "CallMethodViaReflection", "reflect / java_method / try_reflect (calls a JVM method)",
      "PythonUDF", "Python UDFs",
      "PythonUDAF", "Python UDFs",
      "PythonUDTF", "Python UDTFs");

  /** Configuration prefixes a SET may not change. */
  static final Set<String> FENCED_CONFS = Set.of(
      "spark.sql.catalog.", "spark.sql.extensions", "spark.jars", "spark.files",
      "spark.archives", "spark.sql.warehouse.dir", "spark.hadoop.", "spark.sql.hive.",
      "spark.plugins", "spark.driver.", "spark.executor.", "spark.connect.",
      "spark.sql.streaming.", "spark.submit.", "spark.api.mode");

  @Override
  public BoxedUnit apply(SparkSessionExtensions extensions) {
    scala.Function1<Object, BoxedUnit> rule = plan -> {
      if (armed) {
        check((LogicalPlan) plan);
      }
      return BoxedUnit.UNIT;
    };
    scala.Function1<Object, scala.Function1<Object, BoxedUnit>> builder = session -> rule;
    extensions.injectCheckRule((scala.Function1) builder);
    return BoxedUnit.UNIT;
  }

  /** Raises naming the first fenced feature anywhere in `root`, subqueries included. */
  static void check(LogicalPlan root) {
    Deque<LogicalPlan> plans = new ArrayDeque<>();
    plans.push(root);
    while (!plans.isEmpty()) {
      LogicalPlan plan = plans.pop();
      String node = plan.getClass().getSimpleName();
      String why = FENCED_NODES.get(node);
      if (why == null) {
        why = byPath(plan, node);
      }
      if (why != null) {
        throw new UnsupportedOperationException(NOT_AVAILABLE + ": " + why);
      }
      for (Object e : CollectionConverters.asJava(plan.expressions())) {
        checkExpression((Expression) e);
      }
      for (Object child : CollectionConverters.asJava(plan.children())) {
        plans.push((LogicalPlan) child);
      }
      for (Object sub : CollectionConverters.asJava(plan.subqueries())) {
        plans.push((LogicalPlan) sub);
      }
      for (Object inner : CollectionConverters.asJava(plan.innerChildren())) {
        if (inner instanceof LogicalPlan) {
          plans.push((LogicalPlan) inner);
        }
      }
    }
  }

  static void checkExpression(Expression root) {
    Deque<Expression> todo = new ArrayDeque<>();
    todo.push(root);
    while (!todo.isEmpty()) {
      Expression e = todo.pop();
      String why = FENCED_EXPRESSIONS.get(e.getClass().getSimpleName());
      if (why != null) {
        throw new UnsupportedOperationException(NOT_AVAILABLE + ": " + why);
      }
      for (Object child : CollectionConverters.asJava(e.children())) {
        todo.push((Expression) child);
      }
    }
  }

  /** Why `plan` reaches a file path, URL or fenced conf, or null when it does not. */
  static String byPath(LogicalPlan plan, String node) {
    switch (node) {
      case "LogicalRelation":
        // a catalog table carries its CatalogTable; parquet.`/x` and read.load(path) do not
        return defined(call(plan, "catalogTable")) || underWarehouse(call(plan, "relation"))
            ? null
            : "reading files by path (DBFS and cloud paths are not provided; use a table)";
      case "DataSourceV2Relation":
        return defined(call(plan, "identifier")) ? null
            : "a data source by path or options (use a table)";
      case "SetCommand": {
        Object kv = call(plan, "kv");
        if (defined(kv)) {
          Object pair = ((scala.Option<?>) kv).get();
          String key = String.valueOf(((scala.Tuple2<?, ?>) pair)._1());
          Object value = ((scala.Tuple2<?, ?>) pair)._2();
          if (defined(value)) {
            for (String prefix : FENCED_CONFS) {
              if (key.startsWith(prefix)) {
                return "SET " + key;
              }
            }
          }
        }
        return null;
      }
      default:
        break;
    }
    // CREATE TABLE ... LOCATION / OPTIONS (path ...), V2 (tableSpec) and V1 (table.storage)
    Object spec = call(plan, "tableSpec");
    if (spec != null && (defined(call(spec, "location")) || hasPath(call(spec, "options")))) {
      return "LOCATION (a table at a file path; create a managed table)";
    }
    Object table = call(plan, "table");
    if (table != null && table.getClass().getSimpleName().equals("CatalogTable")) {
      Object storage = call(table, "storage");
      if (storage != null && defined(call(storage, "locationUri"))) {
        return "LOCATION (a table at a file path; create a managed table)";
      }
    }
    return null;
  }

  /** Is `relation` a file relation whose every root path is below the warehouse? */
  static boolean underWarehouse(Object relation) {
    Object location = relation == null ? null : call(relation, "location");
    Object roots = location == null ? null : call(location, "rootPaths");
    if (!(roots instanceof scala.collection.Seq) || warehouse == null) {
      return false;
    }
    java.util.List<?> paths = CollectionConverters.asJava((scala.collection.Seq<?>) roots);
    if (paths.isEmpty()) {
      return false;
    }
    for (Object root : paths) {
      String path = pathOf(String.valueOf(root));
      if (!path.startsWith(warehouse)) {
        return false;
      }
    }
    return true;
  }

  static boolean hasPath(Object options) {
    if (options instanceof scala.collection.Map) {
      @SuppressWarnings("unchecked")
      scala.collection.Map<Object, ?> map = (scala.collection.Map<Object, ?>) options;
      return map.contains("path");
    }
    return false;
  }

  static boolean defined(Object option) {
    return option instanceof scala.Option && ((scala.Option<?>) option).isDefined();
  }

  /** `target.name()` when it has such a no-argument method, else null. */
  static Object call(Object target, String name) {
    try {
      Method m = target.getClass().getMethod(name);
      return m.invoke(target);
    } catch (ReflectiveOperationException | RuntimeException e) {
      return null;
    }
  }
}
